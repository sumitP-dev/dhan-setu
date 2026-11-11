from fastapi import APIRouter, status

from app.common.schemas import error_responses
from app.modules.auth.dependencies import CurrentUser
from app.modules.kyc.schemas import KycRead
from app.modules.kyc.service import kyc_read

router = APIRouter(
    prefix="/kyc",
    tags=["kyc"],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)


@router.get("")
async def get_kyc(user: CurrentUser) -> KycRead:
    """The user's KYC status. Cards, payments, UPI and bills need "verified"."""
    return kyc_read(user.kyc_status)
