"""Read-only admin views of users and cards. Admin portal tokens only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.common.enums import CardNetwork, CardStatus, CardType, KycStatus, UserStatus
from app.common.schemas import error_responses
from app.modules.admin.dependencies import AdminConsoleServiceDep
from app.modules.admin.schemas import AdminCardItem, AdminUserDetail, AdminUserItem, Page
from app.modules.admin_auth.dependencies import CurrentAdmin

router = APIRouter(
    prefix="/admin",
    tags=["admin: users and cards"],
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)

SearchQuery = Annotated[
    str | None,
    Query(
        alias="q",
        min_length=2,
        max_length=100,
        description="Name, email or phone (cards: also the last 4 digits).",
    ),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100_000)]


@router.get("/users")
async def list_users(
    admin: CurrentAdmin,
    service: AdminConsoleServiceDep,
    q: SearchQuery = None,
    kyc_status: KycStatus | None = None,
    account_status: Annotated[UserStatus | None, Query(alias="status")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[AdminUserItem]:
    """Everyone who signed up, newest first. Searches are recorded."""
    return await service.users(
        admin, query=q, kyc_status=kyc_status, status=account_status, limit=limit, offset=offset
    )


@router.get("/users/{user_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
async def get_user(
    user_id: uuid.UUID, admin: CurrentAdmin, service: AdminConsoleServiceDep
) -> AdminUserDetail:
    """One user: profile, KYC summary and all cards (removed ones too). Recorded."""
    return await service.user(admin, user_id)


@router.get("/cards")
async def list_cards(  # noqa: PLR0913  (query parameters)
    *,
    admin: CurrentAdmin,
    service: AdminConsoleServiceDep,
    q: SearchQuery = None,
    card_status: Annotated[CardStatus | None, Query(alias="status")] = None,
    network: CardNetwork | None = None,
    card_type: CardType | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[AdminCardItem]:
    """All saved cards, newest first, with their owners. Searches are recorded."""
    return await service.cards(
        admin,
        query=q,
        status=card_status,
        network=network,
        card_type=card_type,
        limit=limit,
        offset=offset,
    )
