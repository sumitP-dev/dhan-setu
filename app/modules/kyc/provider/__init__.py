from app.core.config import Settings
from app.modules.kyc.provider.base import KycProvider
from app.modules.kyc.provider.fake import FakeKycProvider
from app.modules.kyc.provider.unavailable import UnavailableKycProvider


def create_kyc_provider(settings: Settings) -> KycProvider:
    """Settings validation has already refused the fake provider in production."""
    if settings.kyc_provider == "fake":
        if settings.fake_kyc_pages_enabled:
            return FakeKycProvider(
                consent_base_url=f"{settings.public_base_url.rstrip('/')}"
                f"{settings.api_v1_prefix}/kyc/dev/digilocker"
            )
        return FakeKycProvider()
    return UnavailableKycProvider()


__all__ = ["KycProvider", "create_kyc_provider"]
