"""The selfie step: the app runs the provider's selfie SDK, and the provider checks that
a live person is in front of the camera and that the face matches the Aadhaar photo.

The app never reports the result. It only gets a token to start the SDK and then asks
us to check; we fetch the liveness and match score from the provider ourselves.
"""

import uuid
from datetime import timedelta

from app.common.enums import KycStatus
from app.common.exceptions import ConflictError, NotFoundError, TooManyRequestsError
from app.core.config import Settings
from app.core.database import utcnow
from app.core.encryption import FieldCipher
from app.modules.kyc.models import KycSelfieSession
from app.modules.kyc.provider.base import (
    KycProvider,
    KycProviderUnavailableError,
    SelfieResult,
    SelfieStatus,
)
from app.modules.kyc.repository import KycRepository

SELFIE_FAILURE_MESSAGES: dict[str, str] = {
    "selfie_not_live": (
        "We couldn't confirm a live selfie. Try again in good light, without a mask or glasses."
    ),
    "face_mismatch": "Your selfie doesn't match your Aadhaar photo. Try again.",
    "face_not_detected": "We couldn't see your face clearly. Try again in good light.",
    "multiple_faces": "Make sure only you are in the picture, then try again.",
    "session_expired": "The selfie session timed out. Start again.",
    "aadhaar_changed": "Your Aadhaar details were fetched again. Take a new selfie.",
    "kyc_already_submitted": "Your KYC was already submitted.",
    "selfie_failed": "The selfie check couldn't complete. Try again.",
}
# Provider reasons we pass on as they are; anything else becomes "selfie_failed".
_PROVIDER_REASONS = frozenset({"face_not_detected", "multiple_faces"})


class KycSelfieService:
    def __init__(
        self,
        repository: KycRepository,
        provider: KycProvider,
        cipher: FieldCipher | None,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._cipher = cipher
        self._settings = settings

    async def start(self, user_id: uuid.UUID) -> tuple[KycSelfieSession, str]:
        """A selfie session and its SDK token. Reuses the open session if there is one."""
        cipher = self._require_cipher()
        user = await self._repository.lock_user(user_id)
        if user is None:
            raise NotFoundError("User not found", code="user_not_found")
        profile = await self._repository.get_profile(user_id)
        if user.kyc_status is not KycStatus.IN_PROGRESS or profile is None:
            raise ConflictError("Complete DigiLocker first.", code="kyc_selfie_not_expected")
        if not profile.pan_verified:
            raise ConflictError("Verify your PAN first.", code="kyc_selfie_not_expected")
        if profile.selfie_session_id is not None:
            raise ConflictError("Your selfie is already verified.", code="selfie_already_verified")

        now = utcnow()
        open_selfie = await self._repository.open_selfie(
            user_id, profile.digilocker_session_id, now
        )
        if open_selfie is not None:
            return open_selfie, self._decrypt_token(cipher, open_selfie)
        if (
            await self._repository.count_selfies_since(user_id, now - timedelta(days=1))
            >= self._settings.kyc_selfie_attempts_per_day
        ):
            raise TooManyRequestsError(
                "Too many selfie attempts today. Try again tomorrow.",
                code="selfie_attempt_limit",
                retry_after_seconds=int(timedelta(hours=1).total_seconds()),
            )

        digilocker = await self._repository.get_session(profile.digilocker_session_id, user_id)
        if digilocker is None:
            raise ConflictError("Complete DigiLocker first.", code="kyc_selfie_not_expected")
        selfie_id = uuid.uuid4()
        created = await self._provider.create_selfie_session(
            digilocker_session_id=digilocker.provider_session_id,
            reference=f"kyc_{selfie_id.hex}",
        )
        selfie = KycSelfieSession(
            id=selfie_id,
            user_id=user_id,
            digilocker_session_id=digilocker.id,
            provider_session_id=created.id,
            sdk_token_encrypted=cipher.encrypt(
                created.sdk_token, context=self._token_context(user_id, selfie_id)
            ),
            status=SelfieStatus.PENDING,
            expires_at=created.expires_at,
        )
        self._repository.add_selfie(selfie)
        await self._repository.flush()
        return selfie, created.sdk_token

    async def get(self, user_id: uuid.UUID, selfie_id: uuid.UUID) -> KycSelfieSession:
        """The session, after checking with the provider if it's still open."""
        selfie = await self._repository.get_selfie(selfie_id, user_id, for_update=True)
        if selfie is None:
            raise NotFoundError("Selfie session not found", code="kyc_session_not_found")
        if selfie.status is SelfieStatus.PENDING:
            result = await self._provider.fetch_selfie_result(selfie.provider_session_id)
            if result.status is not SelfieStatus.PENDING:
                await self._apply(selfie, result)
                await self._repository.flush()
        return selfie

    async def _apply(self, selfie: KycSelfieSession, result: SelfieResult) -> None:
        selfie.is_live = result.is_live
        selfie.match_score = result.match_score
        selfie.completed_at = utcnow()
        if result.status is SelfieStatus.EXPIRED:
            self._fail(selfie, SelfieStatus.EXPIRED, "session_expired")
            return
        if result.status is not SelfieStatus.COMPLETED:
            reason = result.failure_reason
            self._fail(
                selfie,
                SelfieStatus.FAILED,
                reason if reason in _PROVIDER_REASONS else "selfie_failed",
            )
            return
        if result.is_live is not True:
            self._fail(selfie, SelfieStatus.FAILED, "selfie_not_live")
            return
        if (
            result.match_score is None
            or result.match_score < self._settings.kyc_selfie_min_match_score
        ):
            self._fail(selfie, SelfieStatus.FAILED, "face_mismatch")
            return

        user = await self._repository.lock_user(selfie.user_id)
        profile = await self._repository.get_profile(selfie.user_id, for_update=True)
        if user is None or user.kyc_status is not KycStatus.IN_PROGRESS or profile is None:
            # Submitted, approved or rejected meanwhile: nothing under review changes.
            self._fail(selfie, SelfieStatus.FAILED, "kyc_already_submitted")
            return
        if profile.digilocker_session_id != selfie.digilocker_session_id:
            # DigiLocker ran again after this selfie started: it was matched against
            # an older Aadhaar photo.
            self._fail(selfie, SelfieStatus.FAILED, "aadhaar_changed")
            return
        selfie.status = SelfieStatus.COMPLETED
        selfie.failure_reason = None
        profile.selfie_session_id = selfie.id

    @staticmethod
    def _fail(selfie: KycSelfieSession, status: SelfieStatus, reason: str) -> None:
        selfie.status = status
        selfie.failure_reason = reason

    def _require_cipher(self) -> FieldCipher:
        if self._cipher is None:
            raise KycProviderUnavailableError(code="kyc_not_available")
        return self._cipher

    @staticmethod
    def _token_context(user_id: uuid.UUID, selfie_id: uuid.UUID) -> str:
        return f"kyc:{user_id}:selfie_token:{selfie_id}"

    def _decrypt_token(self, cipher: FieldCipher, selfie: KycSelfieSession) -> str:
        return cipher.decrypt(
            selfie.sdk_token_encrypted, context=self._token_context(selfie.user_id, selfie.id)
        )
