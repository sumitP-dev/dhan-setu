import uuid
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import CardStatus
from app.modules.cards.models import Card, CardVerification


class CardRepository:
    """Every query is scoped to one user, so one user can never reach another's cards."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _active(self, user_id: uuid.UUID) -> Select[Card]:
        return select(Card).where(Card.user_id == user_id, Card.status == CardStatus.ACTIVE)

    async def list_active(self, user_id: uuid.UUID) -> list[Card]:
        result = await self._session.scalars(
            self._active(user_id).order_by(Card.is_default.desc(), Card.created_at.desc())
        )
        return list(result)

    async def get_active(self, user_id: uuid.UUID, card_id: uuid.UUID) -> Card | None:
        return await self._session.scalar(self._active(user_id).where(Card.id == card_id))

    async def get_by_id(self, user_id: uuid.UUID, card_id: uuid.UUID) -> Card | None:
        return await self._session.scalar(
            select(Card).where(Card.user_id == user_id, Card.id == card_id)
        )

    async def get_active_by_token(self, user_id: uuid.UUID, token_id: str) -> Card | None:
        return await self._session.scalar(
            self._active(user_id).where(Card.gateway_token_id == token_id)
        )

    async def get_default(self, user_id: uuid.UUID) -> Card | None:
        return await self._session.scalar(self._active(user_id).where(Card.is_default))

    async def newest_active(self, user_id: uuid.UUID) -> Card | None:
        return await self._session.scalar(
            self._active(user_id).order_by(Card.created_at.desc()).limit(1)
        )

    async def count_active(self, user_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(Card)
            .where(Card.user_id == user_id, Card.status == CardStatus.ACTIVE)
        )
        return count or 0

    async def add(self, card: Card) -> bool:
        """False if it breaks a unique rule (card already saved, or a second default)."""
        try:
            async with self._session.begin_nested():
                self._session.add(card)
        except IntegrityError:
            return False
        return True

    async def flush(self) -> None:
        await self._session.flush()


class CardVerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_key(self, user_id: uuid.UUID, key: str) -> CardVerification | None:
        return await self._session.scalar(
            select(CardVerification).where(
                CardVerification.user_id == user_id, CardVerification.idempotency_key == key
            )
        )

    async def get(
        self, user_id: uuid.UUID, verification_id: uuid.UUID, *, for_update: bool = False
    ) -> CardVerification | None:
        query = select(CardVerification).where(
            CardVerification.user_id == user_id, CardVerification.id == verification_id
        )
        if for_update:
            # Completing twice at once (app retry, webhook) waits here instead of
            # charging, saving or refunding twice.
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def get_by_order_id(
        self, order_id: str, *, for_update: bool = False
    ) -> CardVerification | None:
        """For webhooks, which have no user."""
        query = select(CardVerification).where(CardVerification.gateway_order_id == order_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def get_by_gateway_payment_id(
        self, gateway_payment_id: str, *, for_update: bool = False
    ) -> CardVerification | None:
        query = select(CardVerification).where(
            CardVerification.gateway_payment_id == gateway_payment_id
        )
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def count_since(self, user_id: uuid.UUID, since: datetime) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(CardVerification)
            .where(CardVerification.user_id == user_id, CardVerification.created_at >= since)
        )
        return count or 0

    async def add_if_absent(self, verification: CardVerification) -> bool:
        try:
            async with self._session.begin_nested():
                self._session.add(verification)
        except IntegrityError:
            return False
        return True

    async def flush(self) -> None:
        await self._session.flush()
