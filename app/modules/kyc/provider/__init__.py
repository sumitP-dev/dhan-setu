from app.core.config import Settings
from app.modules.kyc.provider.base import KycProvider
from app.modules.kyc.provider.fake import FakeKycProvider
from app.modules.kyc.provider.unavailable import UnavailableKycProvider


def create_kyc_provider(settings: Settings) -> KycProvider:
    """Settings validation has already refused the fake provider in production."""
    if settings.kyc_provider == "fake":
        return FakeKycProvider()
    return UnavailableKycProvider()


__all__ = ["KycProvider", "create_kyc_provider"]
