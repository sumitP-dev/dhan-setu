import re
import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.common.enums import CardNetwork, CardStatus, CardType, KycStatus, UserStatus
from app.modules.admin.models import AdminAuditLog
from app.modules.cards.models import Card
from app.modules.kyc.models import KycEvent
from app.modules.users.models import User

_DIGITS = re.compile(r"\D")
_PHONE_QUERY = re.compile(r"[+\d\s-]+")
MIN_PHONE_DIGITS = 4
LAST4_LENGTH = 4


def _like(text: str) -> str:
    """A LIKE pattern matching `text` anywhere, with its own % and _ taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _user_search(query: str) -> ColumnElement[bool]:
    """Phone numbers when the query looks like one (at least 4 digits), else name or email."""
    text = query.strip()
    digits = _DIGITS.sub("", text)
    if _PHONE_QUERY.fullmatch(text) and len(digits) >= MIN_PHONE_DIGITS:
        return User.phone.like(_like(digits), escape="\\")
    pattern = _like(text.lower())
    return or_(
        func.lower(User.full_name).like(pattern, escape="\\"),
        User.email.like(pattern, escape="\\"),
    )


class AdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- Users

    def _active_cards(self) -> Select[uuid.UUID, int]:
        return (
            select(Card.user_id, func.count().label("active_cards"))
            .where(Card.status == CardStatus.ACTIVE)
            .group_by(Card.user_id)
        )

    async def list_users(
        self,
        *,
        query: str | None,
        kyc_status: KycStatus | None,
        status: UserStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[User, int]], int]:
        """Newest sign-ups first, with each user's number of active cards."""
        conditions: list[ColumnElement[bool]] = []
        if query:
            conditions.append(_user_search(query))
        if kyc_status is not None:
            conditions.append(User.kyc_status == kyc_status)
        if status is not None:
            conditions.append(User.status == status)

        cards = self._active_cards().subquery()
        result = await self._session.execute(
            select(User, func.coalesce(cards.c.active_cards, 0))
            .outerjoin(cards, cards.c.user_id == User.id)
            .where(*conditions)
            .order_by(User.created_at.desc(), User.id)
            .limit(limit)
            .offset(offset)
        )
        total = await self._session.scalar(
            select(func.count()).select_from(User).where(*conditions)
        )
        return [(row[0], int(row[1])) for row in result.all()], total or 0

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def user_cards(self, user_id: uuid.UUID) -> list[Card]:
        result = await self._session.scalars(
            select(Card)
            .where(Card.user_id == user_id)
            .order_by((Card.status == CardStatus.ACTIVE).desc(), Card.created_at.desc())
        )
        return list(result)

    async def latest_kyc_event(
        self, user_id: uuid.UUID, statuses: tuple[KycStatus, ...]
    ) -> tuple[KycEvent, str | None] | None:
        """The newest event moving to one of `statuses`, with the deciding admin's name."""
        actor = aliased(User)
        row = (
            await self._session.execute(
                select(KycEvent, actor.full_name)
                .outerjoin(actor, actor.id == KycEvent.actor_id)
                .where(KycEvent.user_id == user_id, KycEvent.to_status.in_(statuses))
                .order_by(KycEvent.created_at.desc(), KycEvent.id.desc())
                .limit(1)
            )
        ).first()
        return (row[0], row[1]) if row else None

    # ---- Cards

    async def list_cards(
        self,
        *,
        query: str | None,
        status: CardStatus | None,
        network: CardNetwork | None,
        card_type: CardType | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[Card, User]], int]:
        """Newest cards first, each with its owner."""
        conditions: list[ColumnElement[bool]] = []
        if query:
            text = query.strip()
            if text.isdigit() and len(text) == LAST4_LENGTH:
                conditions.append(or_(Card.last4 == text, _user_search(text)))
            else:
                conditions.append(_user_search(text))
        if status is not None:
            conditions.append(Card.status == status)
        if network is not None:
            conditions.append(Card.network == network)
        if card_type is not None:
            conditions.append(Card.card_type == card_type)

        base = select(Card, User).join(User, User.id == Card.user_id).where(*conditions)
        result = await self._session.execute(
            base.order_by(Card.created_at.desc(), Card.id).limit(limit).offset(offset)
        )
        total = await self._session.scalar(
            select(func.count())
            .select_from(Card)
            .join(User, User.id == Card.user_id)
            .where(*conditions)
        )
        return [(row[0], row[1]) for row in result.all()], total or 0

    # ---- Audit

    def add_audit(self, log: AdminAuditLog) -> None:
        self._session.add(log)

    async def flush(self) -> None:
        await self._session.flush()


def is_expired(month: int, year: int, now: datetime) -> bool:
    """Cards work until the end of their expiry month."""
    return (year, month) < (now.year, now.month)
