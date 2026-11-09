from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.webhooks.models import WebhookEvent


class WebhookEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def seen(self, provider: str, event_id: str | None, payload_hash: str) -> bool:
        conditions = [WebhookEvent.payload_hash == payload_hash]
        if event_id is not None:
            conditions.append(
                (WebhookEvent.provider == provider) & (WebhookEvent.event_id == event_id)
            )
        found = await self._session.scalar(select(WebhookEvent.id).where(or_(*conditions)))
        return found is not None

    async def add_if_new(self, event: WebhookEvent) -> bool:
        """False if this event (same id or same body) was recorded in the meantime."""
        try:
            async with self._session.begin_nested():
                self._session.add(event)
        except IntegrityError:
            return False
        return True

    async def flush(self) -> None:
        await self._session.flush()
