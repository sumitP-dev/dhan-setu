"""Admin endpoints for reviewing KYC. Admins only (role "admin")."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.common.enums import KycStatus
from app.common.schemas import error_responses
from app.modules.kyc.dependencies import AdminUser, KycReviewServiceDep
from app.modules.kyc.schemas import KycReject, KycReviewDetail, KycReviewPage

router = APIRouter(
    prefix="/admin/kyc",
    tags=["admin: kyc"],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)


@router.get("", responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT))
async def review_queue(
    _admin: AdminUser,
    service: KycReviewServiceDep,
    kyc_status: Annotated[KycStatus, Query(alias="status")] = KycStatus.PENDING_REVIEW,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> KycReviewPage:
    """Users in a KYC status (default: waiting for review), the longest-waiting first."""
    return await service.queue(kyc_status, limit=limit, offset=offset)


@router.get(
    "/{user_id}",
    responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def review_detail(
    user_id: uuid.UUID, admin: AdminUser, service: KycReviewServiceDep
) -> KycReviewDetail:
    """A user's KYC details, checks and history. Each opening is recorded."""
    return await service.detail(admin, user_id)


@router.post(
    "/{user_id}/approve",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT
    ),
)
async def approve_kyc(
    user_id: uuid.UUID, admin: AdminUser, service: KycReviewServiceDep
) -> KycReviewDetail:
    """Approve a KYC waiting for review. The user can then use cards and payments."""
    return await service.approve(admin, user_id)


@router.post(
    "/{user_id}/reject",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT
    ),
)
async def reject_kyc(
    user_id: uuid.UUID, body: KycReject, admin: AdminUser, service: KycReviewServiceDep
) -> KycReviewDetail:
    """Reject a KYC waiting for review. The user sees the reason's message (not the
    note) and can start again."""
    return await service.reject(admin, user_id, body)
