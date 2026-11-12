from typing import Annotated

from fastapi import Depends, Request

from app.common.enums import KycStatus
from app.common.exceptions import ForbiddenError
from app.core.config import AppSettings
from app.core.database import DbSession
from app.core.encryption import FieldCipher
from app.modules.admin_auth.dependencies import CurrentAdmin
from app.modules.auth.dependencies import CurrentUser
from app.modules.kyc.documents import KycDocumentService
from app.modules.kyc.provider import KycProvider
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.review import KycReviewService
from app.modules.kyc.selfie import KycSelfieService
from app.modules.kyc.service import KycService
from app.modules.users.models import User


def get_kyc_provider(request: Request) -> KycProvider:
    provider: KycProvider = request.app.state.kyc_provider
    return provider


KycProviderDep = Annotated[KycProvider, Depends(get_kyc_provider)]


def get_kyc_service(db: DbSession) -> KycService:
    return KycService(KycRepository(db))


KycServiceDep = Annotated[KycService, Depends(get_kyc_service)]


def get_field_cipher(request: Request) -> FieldCipher | None:
    cipher: FieldCipher | None = request.app.state.field_cipher
    return cipher


def get_kyc_document_service(
    db: DbSession,
    provider: KycProviderDep,
    cipher: Annotated[FieldCipher | None, Depends(get_field_cipher)],
    settings: AppSettings,
) -> KycDocumentService:
    repository = KycRepository(db)
    return KycDocumentService(repository, KycService(repository), provider, cipher, settings)


KycDocumentServiceDep = Annotated[KycDocumentService, Depends(get_kyc_document_service)]


def get_kyc_selfie_service(
    db: DbSession,
    provider: KycProviderDep,
    cipher: Annotated[FieldCipher | None, Depends(get_field_cipher)],
    settings: AppSettings,
) -> KycSelfieService:
    return KycSelfieService(KycRepository(db), provider, cipher, settings)


KycSelfieServiceDep = Annotated[KycSelfieService, Depends(get_kyc_selfie_service)]


def get_kyc_review_service(
    db: DbSession,
    provider: KycProviderDep,
    cipher: Annotated[FieldCipher | None, Depends(get_field_cipher)],
    settings: AppSettings,
) -> KycReviewService:
    repository = KycRepository(db)
    status = KycService(repository)
    documents = KycDocumentService(repository, status, provider, cipher, settings)
    return KycReviewService(repository, status, documents, settings)


KycReviewServiceDep = Annotated[KycReviewService, Depends(get_kyc_review_service)]

# Admin portal tokens only (not customer-app tokens), with a live admin session.
AdminUser = CurrentAdmin


class KycRequiredError(ForbiddenError):
    code = "kyc_required"
    message = "Complete your KYC to use this feature."


async def require_kyc_verified(user: CurrentUser) -> User:
    """Cards, payments, UPI and bills are only for users whose KYC is verified."""
    if user.kyc_status is not KycStatus.VERIFIED:
        raise KycRequiredError
    return user


VerifiedUser = Annotated[User, Depends(require_kyc_verified)]
