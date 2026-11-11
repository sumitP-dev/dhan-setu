"""Used when no KYC provider is set up (KYC_PROVIDER=none). Every call fails with "not
available", so nobody can be verified and features that need KYC stay locked."""

from datetime import date
from typing import NoReturn

from app.modules.kyc.provider.base import (
    DigiLockerResult,
    DigiLockerSession,
    KycProviderUnavailableError,
    PanVerification,
    SelfieResult,
    SelfieSession,
)


def _unavailable() -> NoReturn:
    raise KycProviderUnavailableError(code="kyc_not_available")


class UnavailableKycProvider:
    async def create_digilocker_session(
        self, *, reference: str, redirect_url: str
    ) -> DigiLockerSession:
        _unavailable()

    async def fetch_digilocker_result(self, session_id: str) -> DigiLockerResult:
        _unavailable()

    async def verify_pan(
        self, *, pan: str, name: str, date_of_birth: date | None
    ) -> PanVerification:
        _unavailable()

    async def create_selfie_session(
        self, *, digilocker_session_id: str, reference: str
    ) -> SelfieSession:
        _unavailable()

    async def fetch_selfie_result(self, session_id: str) -> SelfieResult:
        _unavailable()

    def verify_webhook_signature(self, body: bytes, signature: str | None) -> bool:
        return False

    async def aclose(self) -> None:
        return None
