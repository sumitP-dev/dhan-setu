"""Read-only views of users and cards for the admin portal."""

import logging
import uuid

from app.common.enums import CardNetwork, CardStatus, CardType, KycStatus, UserStatus
from app.common.exceptions import NotFoundError
from app.core.database import utcnow
from app.modules.admin.models import AdminAuditLog
from app.modules.admin.repository import AdminRepository, is_expired
from app.modules.admin.schemas import (
    AdminCardItem,
    AdminCardOwner,
    AdminCardRead,
    AdminKycSummary,
    AdminUserDetail,
    AdminUserItem,
    Page,
)
from app.modules.cards.models import Card
from app.modules.users.models import User

logger = logging.getLogger(__name__)

_DECISIONS = (KycStatus.VERIFIED, KycStatus.REJECTED)


def _card(card: Card) -> AdminCardRead:
    return AdminCardRead(
        id=card.id,
        last4=card.last4,
        network=card.network,
        card_type=card.card_type,
        issuer=card.issuer,
        expiry_month=card.expiry_month,
        expiry_year=card.expiry_year,
        expired=is_expired(card.expiry_month, card.expiry_year, utcnow()),
        is_default=card.is_default and card.status is CardStatus.ACTIVE,
        status=card.status,
        created_at=card.created_at,
        removed_at=card.removed_at,
    )


def _user_item(user: User, active_cards: int) -> AdminUserItem:
    return AdminUserItem(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        role=user.role,
        status=user.status,
        kyc_status=user.kyc_status,
        active_cards=active_cards,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


class AdminConsoleService:
    def __init__(self, repository: AdminRepository) -> None:
        self._repository = repository

    async def users(
        self,
        admin: User,
        *,
        query: str | None,
        kyc_status: KycStatus | None,
        status: UserStatus | None,
        limit: int,
        offset: int,
    ) -> Page[AdminUserItem]:
        if query:
            await self._audit(admin, "search_users", detail=query)
        rows, total = await self._repository.list_users(
            query=query, kyc_status=kyc_status, status=status, limit=limit, offset=offset
        )
        return Page[AdminUserItem](
            items=[_user_item(user, cards) for user, cards in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def user(self, admin: User, user_id: uuid.UUID) -> AdminUserDetail:
        user = await self._repository.get_user(user_id)
        if user is None:
            raise NotFoundError("User not found", code="user_not_found")
        await self._audit(admin, "view_user", target_user_id=user_id)

        cards = await self._repository.user_cards(user_id)
        submitted = await self._repository.latest_kyc_event(user_id, (KycStatus.PENDING_REVIEW,))
        decided = await self._repository.latest_kyc_event(user_id, _DECISIONS)
        decision, decided_by = decided if decided else (None, None)
        rejected = decision is not None and decision.to_status is KycStatus.REJECTED
        item = _user_item(user, sum(card.status is CardStatus.ACTIVE for card in cards))
        return AdminUserDetail(
            **item.model_dump(),
            kyc=AdminKycSummary(
                status=user.kyc_status,
                submitted_at=submitted[0].created_at if submitted else None,
                decided_at=decision.created_at if decision else None,
                decided_by=decided_by,
                rejection_reason=decision.reason if decision and rejected else None,
                rejection_note=decision.note if decision and rejected else None,
            ),
            cards=[_card(card) for card in cards],
        )

    async def cards(
        self,
        admin: User,
        *,
        query: str | None,
        status: CardStatus | None,
        network: CardNetwork | None,
        card_type: CardType | None,
        limit: int,
        offset: int,
    ) -> Page[AdminCardItem]:
        if query:
            await self._audit(admin, "search_cards", detail=query)
        rows, total = await self._repository.list_cards(
            query=query,
            status=status,
            network=network,
            card_type=card_type,
            limit=limit,
            offset=offset,
        )
        items = [
            AdminCardItem(
                **_card(card).model_dump(),
                owner=AdminCardOwner(
                    id=owner.id,
                    full_name=owner.full_name,
                    phone=owner.phone,
                    kyc_status=owner.kyc_status,
                ),
            )
            for card, owner in rows
        ]
        return Page[AdminCardItem](items=items, total=total, limit=limit, offset=offset)

    async def _audit(
        self,
        admin: User,
        action: str,
        *,
        target_user_id: uuid.UUID | None = None,
        detail: str | None = None,
    ) -> None:
        self._repository.add_audit(
            AdminAuditLog(
                admin_id=admin.id,
                action=action,
                target_user_id=target_user_id,
                detail=detail[:200] if detail else None,
            )
        )
        await self._repository.flush()
        logger.info("Admin %s: %s %s", admin.id, action, target_user_id or "")
