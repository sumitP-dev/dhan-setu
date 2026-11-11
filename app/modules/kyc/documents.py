"""Collecting identity documents: Aadhaar (and PAN) from DigiLocker, or PAN typed in.

The app never sends identity data. It only starts a DigiLocker session and asks for the
result; the backend fetches the documents from the provider itself and checks them.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

from sqlalchemy.exc import IntegrityError

from app.common.enums import KycStatus
from app.common.exceptions import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    TooManyRequestsError,
)
from app.core.config import Settings
from app.core.database import utcnow
from app.core.encryption import DecryptionError, FieldCipher
from app.modules.kyc.matching import birth_dates_match, names_match
from app.modules.kyc.models import KycDigiLockerSession, KycProfile
from app.modules.kyc.provider.base import (
    AadhaarDetails,
    DigiLockerResult,
    DigiLockerStatus,
    KycProvider,
    KycProviderError,
    KycProviderUnavailableError,
    PanStatus,
    is_individual_pan,
    mask_aadhaar,
    mask_pan,
    normalize_pan,
)
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.schemas import KycDetails, KycNextStep, KycRead
from app.modules.kyc.service import KycService
from app.modules.kyc.state import CAN_START
from app.modules.users.models import User

MAX_PAN_ATTEMPTS = 5
_STATE_BYTES = 32
_STATE_LENGTH = 43  # token_urlsafe(32)

FAILURE_MESSAGES: dict[str, str] = {
    "consent_denied": "You didn't allow sharing from DigiLocker. Try again to continue.",
    "session_expired": "The DigiLocker session timed out. Start again.",
    "aadhaar_not_verified": "We couldn't verify your Aadhaar from DigiLocker. Try again.",
    "kyc_already_submitted": "Your KYC was already submitted.",
    "digilocker_failed": "DigiLocker couldn't complete. Try again.",
}

PAN_ISSUE_MESSAGES: dict[str, str] = {
    "pan_not_in_digilocker": "Your PAN isn't in DigiLocker. Enter your PAN to continue.",
    "pan_not_individual": "Use your personal PAN (the 4th letter is P).",
    "pan_invalid": "This PAN isn't valid. Check it and try again.",
    "pan_name_mismatch": "The name on your PAN doesn't match your Aadhaar.",
    "pan_dob_mismatch": "The date of birth on your PAN doesn't match your Aadhaar.",
    "pan_already_used": "This PAN is already linked to another Dhan Setu account.",
}


def hash_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def looks_like_state(value: str) -> bool:
    return len(value) == _STATE_LENGTH and value.replace("-", "").replace("_", "").isalnum()


@dataclass(frozen=True, slots=True)
class _PanCheck:
    pan: str
    issue: str | None


class KycDocumentService:
    def __init__(
        self,
        repository: KycRepository,
        status: KycService,
        provider: KycProvider,
        cipher: FieldCipher | None,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._status = status
        self._provider = provider
        self._cipher = cipher
        self._settings = settings

    # ---- Reading

    async def read(self, user: User) -> KycRead:
        profile = await self._repository.get_profile(user.id)
        details = self._details(profile) if profile else None
        return KycRead(
            status=user.kyc_status,
            can_start=user.kyc_status in CAN_START,
            next_step=self._next_step(user.kyc_status, profile),
            details=details,
        )

    @staticmethod
    def _next_step(status: KycStatus, profile: KycProfile | None) -> KycNextStep | None:
        if status in {KycStatus.NOT_SUBMITTED, KycStatus.REJECTED}:
            return "digilocker"
        if status is not KycStatus.IN_PROGRESS:
            return None
        if profile is None:
            return "digilocker"
        return "selfie" if profile.pan_verified else "pan"

    def _details(self, profile: KycProfile) -> KycDetails | None:
        if self._cipher is None:
            return None
        try:
            date_of_birth = self._decrypt(profile, "date_of_birth", profile.date_of_birth_encrypted)
            return KycDetails(
                name=self._decrypt(profile, "name", profile.name_encrypted) or "",
                date_of_birth=date.fromisoformat(date_of_birth) if date_of_birth else None,
                birth_year=profile.birth_year,
                gender=profile.gender,
                address=self._decrypt(profile, "address", profile.address_encrypted),
                aadhaar=mask_aadhaar(profile.aadhaar_last4),
                pan=profile.pan_masked,
                pan_verified=profile.pan_verified,
                pan_issue=profile.pan_issue,
                pan_issue_message=PAN_ISSUE_MESSAGES.get(profile.pan_issue or ""),
                fetched_at=profile.fetched_at,
            )
        except DecryptionError:
            # Wrong key or tampered row: show nothing rather than wrong data.
            return None

    # ---- DigiLocker

    async def start_digilocker(self, user: User) -> KycDigiLockerSession:
        locked = await self._repository.lock_user(user.id)
        if locked is None:
            raise NotFoundError("User not found", code="user_not_found")
        if locked.kyc_status not in CAN_START:
            raise ConflictError("Your KYC is already submitted.", code="kyc_not_startable")

        now = utcnow()
        open_session = await self._repository.open_session(user.id, now)
        if open_session is not None:
            return open_session
        since = now - timedelta(days=1)
        if (
            await self._repository.count_sessions_since(user.id, since)
            >= self._settings.kyc_attempts_per_day
        ):
            raise TooManyRequestsError(
                "Too many attempts today. Try again tomorrow.",
                code="kyc_attempt_limit",
                retry_after_seconds=int(timedelta(hours=1).total_seconds()),
            )

        session_id = uuid.uuid4()
        state = secrets.token_urlsafe(_STATE_BYTES)
        callback = (
            f"{self._settings.public_base_url.rstrip('/')}{self._settings.api_v1_prefix}"
            f"/kyc/digilocker/callback?{urlencode({'state': state})}"
        )
        created = await self._provider.create_digilocker_session(
            reference=f"kyc_{session_id.hex}", redirect_url=callback
        )
        session = KycDigiLockerSession(
            id=session_id,
            user_id=user.id,
            provider_session_id=created.id,
            state_hash=hash_state(state),
            consent_url=created.consent_url,
            status=DigiLockerStatus.PENDING,
            expires_at=created.expires_at,
        )
        self._repository.add_session(session)
        await self._repository.flush()
        return session

    async def get_digilocker(self, user: User, session_id: uuid.UUID) -> KycDigiLockerSession:
        """The session, after checking with the provider if it's still open."""
        session = await self._repository.get_session(session_id, user.id, for_update=True)
        if session is None:
            raise NotFoundError("Verification session not found", code="kyc_session_not_found")
        await self._sync(session)
        return session

    async def handle_callback(self, state: str) -> str:
        """DigiLocker sent the user back. Returns the outcome for the app's deep link;
        never details, and never trusts anything in the URL beyond the state token."""
        if not looks_like_state(state):
            return "invalid"
        session = await self._repository.get_session_by_state(hash_state(state))
        if session is None:
            return "invalid"
        try:
            await self._sync(session)
        except KycProviderError:
            # The app checks again; the session stays open.
            return DigiLockerStatus.PENDING.value
        return session.status.value

    async def _sync(self, session: KycDigiLockerSession) -> None:
        if session.status is not DigiLockerStatus.PENDING:
            return
        result = await self._provider.fetch_digilocker_result(session.provider_session_id)
        if result.status is DigiLockerStatus.PENDING:
            return
        if result.status is DigiLockerStatus.COMPLETED:
            await self._apply_completed(session, result)
        else:
            reason = {
                DigiLockerStatus.DENIED: "consent_denied",
                DigiLockerStatus.EXPIRED: "session_expired",
            }.get(result.status, "digilocker_failed")
            self._finish(session, result.status, reason)
        await self._repository.flush()

    def _finish(
        self, session: KycDigiLockerSession, status: DigiLockerStatus, reason: str | None
    ) -> None:
        session.status = status
        session.failure_reason = reason
        session.completed_at = utcnow()

    async def _apply_completed(
        self, session: KycDigiLockerSession, result: DigiLockerResult
    ) -> None:
        user = await self._repository.lock_user(session.user_id)
        if user is None:
            raise NotFoundError("User not found", code="user_not_found")
        if user.kyc_status not in CAN_START:
            # Submitted or verified meanwhile: details under review must not change.
            self._finish(session, DigiLockerStatus.FAILED, "kyc_already_submitted")
            return
        aadhaar = result.aadhaar
        if aadhaar is None or not aadhaar.signature_verified:
            self._finish(session, DigiLockerStatus.FAILED, "aadhaar_not_verified")
            return

        cipher = self._require_cipher()
        profile = await self._repository.get_profile(user.id, for_update=True)
        if profile is None:
            profile = KycProfile(user_id=user.id, pan_attempts=0)
            self._repository.add_profile(profile)
        self._store_aadhaar(cipher, profile, aadhaar, session)

        if result.pan is None:
            self._clear_pan(profile, issue="pan_not_in_digilocker")
        else:
            issue = None
            if not is_individual_pan(result.pan.number):
                issue = "pan_not_individual"
            elif not names_match(aadhaar.name, result.pan.name):
                issue = "pan_name_mismatch"
            elif (
                birth_dates_match(
                    aadhaar.date_of_birth, aadhaar.birth_year, result.pan.date_of_birth
                )
                is False
            ):
                issue = "pan_dob_mismatch"
            await self._store_pan(
                cipher, profile, _PanCheck(result.pan.number, issue), source="digilocker"
            )

        self._finish(session, DigiLockerStatus.COMPLETED, None)
        await self._flush_profile()
        if user.kyc_status is not KycStatus.IN_PROGRESS:
            await self._status.transition(
                user.id, KycStatus.IN_PROGRESS, reason="digilocker_completed"
            )

    # ---- PAN typed in

    async def submit_pan(self, user: User, raw_pan: str) -> KycRead:
        pan = normalize_pan(raw_pan)
        if pan is None:
            raise InvalidInputError("Enter a valid PAN, like ABCPE1234F.", code="invalid_pan")
        locked = await self._repository.lock_user(user.id)
        if locked is None or locked.kyc_status is not KycStatus.IN_PROGRESS:
            raise ConflictError("Complete DigiLocker first.", code="kyc_pan_not_expected")
        profile = await self._repository.get_profile(user.id, for_update=True)
        if profile is None:
            raise ConflictError("Complete DigiLocker first.", code="kyc_pan_not_expected")
        if profile.pan_verified:
            raise ConflictError("Your PAN is already verified.", code="pan_already_verified")
        if profile.pan_attempts >= MAX_PAN_ATTEMPTS:
            raise TooManyRequestsError(
                "Too many PAN attempts. Contact support.", code="pan_attempt_limit"
            )
        profile.pan_attempts += 1
        if not is_individual_pan(pan):
            await self._store_pan(
                self._require_cipher(), profile, _PanCheck(pan, "pan_not_individual"), "manual"
            )
            await self._flush_profile()
            return await self.read(locked)

        cipher = self._require_cipher()
        name = self._decrypt(profile, "name", profile.name_encrypted) or ""
        dob_text = self._decrypt(profile, "date_of_birth", profile.date_of_birth_encrypted)
        check = await self._provider.verify_pan(
            pan=pan, name=name, date_of_birth=date.fromisoformat(dob_text) if dob_text else None
        )
        issue = None
        if check.status is not PanStatus.VALID:
            issue = "pan_invalid"
        elif not check.name_matches:
            issue = "pan_name_mismatch"
        elif check.date_of_birth_matches is False:
            issue = "pan_dob_mismatch"
        await self._store_pan(cipher, profile, _PanCheck(pan, issue), source="manual")
        await self._flush_profile()
        return await self.read(locked)

    # ---- Storage helpers

    def _require_cipher(self) -> FieldCipher:
        if self._cipher is None:
            raise KycProviderUnavailableError(code="kyc_not_available")
        return self._cipher

    @staticmethod
    def _context(profile: KycProfile, field: str) -> str:
        return f"kyc:{profile.user_id}:{field}"

    def _encrypt(self, cipher: FieldCipher, profile: KycProfile, field: str, value: str) -> str:
        return cipher.encrypt(value, context=self._context(profile, field))

    def _decrypt(self, profile: KycProfile, field: str, value: str | None) -> str | None:
        if value is None or self._cipher is None:
            return None
        return self._cipher.decrypt(value, context=self._context(profile, field))

    def _store_aadhaar(
        self,
        cipher: FieldCipher,
        profile: KycProfile,
        aadhaar: AadhaarDetails,
        session: KycDigiLockerSession,
    ) -> None:
        profile.digilocker_session_id = session.id
        profile.name_encrypted = self._encrypt(cipher, profile, "name", aadhaar.name)
        profile.date_of_birth_encrypted = (
            self._encrypt(cipher, profile, "date_of_birth", aadhaar.date_of_birth.isoformat())
            if aadhaar.date_of_birth
            else None
        )
        profile.birth_year = aadhaar.birth_year
        profile.gender = aadhaar.gender
        profile.address_encrypted = (
            self._encrypt(cipher, profile, "address", aadhaar.address) if aadhaar.address else None
        )
        profile.aadhaar_last4 = aadhaar.last4
        profile.fetched_at = utcnow()

    @staticmethod
    def _clear_pan(profile: KycProfile, *, issue: str) -> None:
        profile.pan_encrypted = None
        profile.pan_masked = None
        profile.pan_hash = None
        profile.pan_source = None
        profile.pan_verified = False
        profile.pan_issue = issue

    async def _store_pan(
        self, cipher: FieldCipher, profile: KycProfile, check: _PanCheck, source: str
    ) -> None:
        issue = check.issue
        pan_hash = cipher.lookup_hash(check.pan, purpose="pan")
        if issue is None and await self._repository.pan_used_by_another_user(
            pan_hash, profile.user_id
        ):
            issue = "pan_already_used"
        profile.pan_encrypted = self._encrypt(cipher, profile, "pan", check.pan)
        profile.pan_masked = mask_pan(check.pan)
        # Only a verified PAN claims the hash, so a mismatched attempt can't block
        # the real owner.
        profile.pan_hash = pan_hash if issue is None else None
        profile.pan_source = source
        profile.pan_verified = issue is None
        profile.pan_issue = issue

    async def _flush_profile(self) -> None:
        try:
            await self._repository.flush()
        except IntegrityError as error:
            # Another account verified the same PAN at this moment. Nothing is saved;
            # trying again hits the duplicate check above.
            raise ConflictError("Something changed. Try again.", code="kyc_conflict") from error
