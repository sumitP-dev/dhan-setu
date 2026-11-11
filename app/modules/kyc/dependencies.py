from typing import Annotated

from fastapi import Depends, Request

from app.common.enums import KycStatus
from app.common.exceptions import ForbiddenError
from app.core.database import DbSession
from app.modules.auth.dependencies import CurrentUser
from app.modules.kyc.provider import KycProvider
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.service import KycService
from app.modules.users.models import User


def get_kyc_provider(request: Request) -> KycProvider:
    provider: KycProvider = request.app.state.kyc_provider
    return provider


KycProviderDep = Annotated[KycProvider, Depends(get_kyc_provider)]


def get_kyc_service(db: DbSession) -> KycService:
    return KycService(KycRepository(db))


KycServiceDep = Annotated[KycService, Depends(get_kyc_service)]


class KycRequiredError(ForbiddenError):
    code = "kyc_required"
    message = "Complete your KYC to use this feature."


async def require_kyc_verified(user: CurrentUser) -> User:
    """Cards, payments, UPI and bills are only for users whose KYC is verified."""
    if user.kyc_status is not KycStatus.VERIFIED:
        raise KycRequiredError
    return user


VerifiedUser = Annotated[User, Depends(require_kyc_verified)]
