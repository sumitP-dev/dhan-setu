"""In-memory KYC provider for tests and local development. Nobody's identity is checked.

It follows the same rules as a real provider (id checks, session expiry, a selfie only
after DigiLocker, signed webhooks with its own secret), so code that works against it
works against the real one. complete_digilocker() and complete_selfie() play the part of
the user in DigiLocker and in the selfie SDK. Settings refuse KYC_PROVIDER=fake in
production.
"""

import secrets
import string
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from app.modules.kyc.provider.base import (
    AadhaarDetails,
    DigiLockerResult,
    DigiLockerSession,
    DigiLockerStatus,
    Gender,
    KycProviderError,
    KycProviderUnavailableError,
    KycSessionNotFoundError,
    PanDetails,
    PanStatus,
    PanVerification,
    SelfieResult,
    SelfieSession,
    SelfieStatus,
    normalize_pan,
    require_reference,
    require_session_id,
)
from app.modules.payments.gateway.signatures import hmac_sha256_hex, signature_matches

SESSION_TTL = timedelta(minutes=15)
MAX_MATCH_SCORE = 100

DEFAULT_AADHAAR = AadhaarDetails(
    last4="1234",
    name="Asha Verma",
    date_of_birth=date(1994, 5, 17),
    birth_year=1994,
    gender=Gender.FEMALE,
    address="12 MG Road, Bengaluru, Karnataka 560001",
    signature_verified=True,
)
DEFAULT_PAN = PanDetails(number="ABCPV1234F", name="ASHA VERMA", date_of_birth=date(1994, 5, 17))


def _new_id(prefix: str) -> str:
    alphabet = string.ascii_letters + string.digits
    return f"{prefix}_" + "".join(secrets.choice(alphabet) for _ in range(16))


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _DigiLocker:
    session: DigiLockerSession
    reference: str
    result: DigiLockerResult


@dataclass(slots=True)
class _Selfie:
    session: SelfieSession
    digilocker_session_id: str
    result: SelfieResult


class FakeKycProvider:
    def __init__(self) -> None:
        self._webhook_secret = secrets.token_hex(32)
        self.unavailable = False  # tests: simulate a network failure
        self.digilocker: dict[str, _DigiLocker] = {}
        self.selfies: dict[str, _Selfie] = {}
        # PANs the fake "Income Tax database" knows: pan -> (name, date of birth).
        self.pan_records: dict[str, tuple[str, date | None]] = {
            DEFAULT_PAN.number: (DEFAULT_PAN.name, DEFAULT_PAN.date_of_birth)
        }

    async def aclose(self) -> None:
        return None

    def _check_available(self) -> None:
        if self.unavailable:
            raise KycProviderUnavailableError

    # ---- DigiLocker

    async def create_digilocker_session(
        self, *, reference: str, redirect_url: str
    ) -> DigiLockerSession:
        self._check_available()
        require_reference(reference)
        if not redirect_url.startswith(("https://", "http://")):
            msg = "redirect_url must be an http(s) URL"
            raise ValueError(msg)
        # Like real providers, the same reference returns the same open session.
        for entry in self.digilocker.values():
            if (
                entry.reference == reference
                and entry.result.status is DigiLockerStatus.PENDING
                and entry.session.expires_at > _now()
            ):
                return entry.session
        session_id = _new_id("dls")
        session = DigiLockerSession(
            id=session_id,
            consent_url=f"https://digilocker.fake.local/consent/{session_id}",
            expires_at=_now() + SESSION_TTL,
        )
        self.digilocker[session_id] = _DigiLocker(
            session=session,
            reference=reference,
            result=DigiLockerResult(session_id=session_id, status=DigiLockerStatus.PENDING),
        )
        return session

    async def fetch_digilocker_result(self, session_id: str) -> DigiLockerResult:
        self._check_available()
        entry = self._digilocker_entry(session_id)
        if entry.result.status is DigiLockerStatus.PENDING and entry.session.expires_at <= _now():
            entry.result = replace(entry.result, status=DigiLockerStatus.EXPIRED)
        return entry.result

    def complete_digilocker(
        self,
        session_id: str,
        *,
        aadhaar: AadhaarDetails = DEFAULT_AADHAAR,
        pan: PanDetails | None = DEFAULT_PAN,
    ) -> None:
        """Tests: the user logs in to DigiLocker and shares Aadhaar (and PAN)."""
        entry = self._digilocker_entry(session_id)
        entry.result = DigiLockerResult(
            session_id=session_id, status=DigiLockerStatus.COMPLETED, aadhaar=aadhaar, pan=pan
        )

    def end_digilocker(self, session_id: str, status: DigiLockerStatus, reason: str) -> None:
        """Tests: the user refuses consent, the session expires or DigiLocker fails."""
        entry = self._digilocker_entry(session_id)
        entry.result = DigiLockerResult(session_id=session_id, status=status, failure_reason=reason)

    def _digilocker_entry(self, session_id: str) -> _DigiLocker:
        require_session_id(session_id)
        entry = self.digilocker.get(session_id)
        if entry is None:
            raise KycSessionNotFoundError
        return entry

    # ---- PAN

    async def verify_pan(
        self, *, pan: str, name: str, date_of_birth: date | None
    ) -> PanVerification:
        self._check_available()
        number = normalize_pan(pan)
        if number is None:
            msg = "pan must be a valid PAN"
            raise ValueError(msg)
        record = self.pan_records.get(number)
        if record is None:
            return PanVerification(
                pan=number,
                status=PanStatus.INVALID,
                name_matches=False,
                date_of_birth_matches=None,
                aadhaar_linked=None,
            )
        record_name, record_dob = record
        dob_matches = None
        if date_of_birth is not None and record_dob is not None:
            dob_matches = date_of_birth == record_dob
        return PanVerification(
            pan=number,
            status=PanStatus.VALID,
            name_matches=" ".join(name.split()).casefold() == record_name.casefold(),
            date_of_birth_matches=dob_matches,
            aadhaar_linked=True,
        )

    # ---- Selfie

    async def create_selfie_session(
        self, *, digilocker_session_id: str, reference: str
    ) -> SelfieSession:
        self._check_available()
        require_reference(reference)
        digilocker = await self.fetch_digilocker_result(digilocker_session_id)
        if digilocker.status is not DigiLockerStatus.COMPLETED:
            # The selfie is matched against the Aadhaar photo, so DigiLocker comes first.
            raise KycProviderError(code="digilocker_not_completed")
        session_id = _new_id("sfs")
        session = SelfieSession(
            id=session_id, sdk_token=secrets.token_urlsafe(32), expires_at=_now() + SESSION_TTL
        )
        self.selfies[session_id] = _Selfie(
            session=session,
            digilocker_session_id=digilocker_session_id,
            result=SelfieResult(session_id=session_id, status=SelfieStatus.PENDING),
        )
        return session

    async def fetch_selfie_result(self, session_id: str) -> SelfieResult:
        self._check_available()
        entry = self._selfie_entry(session_id)
        if entry.result.status is SelfieStatus.PENDING and entry.session.expires_at <= _now():
            entry.result = replace(entry.result, status=SelfieStatus.EXPIRED)
        return entry.result

    def complete_selfie(
        self, session_id: str, *, is_live: bool = True, match_score: float = 92.5
    ) -> None:
        """Tests: the user takes a selfie in the SDK."""
        if not 0 <= match_score <= MAX_MATCH_SCORE:
            msg = "match_score must be 0-100"
            raise ValueError(msg)
        entry = self._selfie_entry(session_id)
        entry.result = SelfieResult(
            session_id=session_id,
            status=SelfieStatus.COMPLETED,
            is_live=is_live,
            match_score=match_score,
        )

    def fail_selfie(self, session_id: str, reason: str = "face_not_detected") -> None:
        entry = self._selfie_entry(session_id)
        entry.result = SelfieResult(
            session_id=session_id, status=SelfieStatus.FAILED, failure_reason=reason
        )

    def _selfie_entry(self, session_id: str) -> _Selfie:
        require_session_id(session_id)
        entry = self.selfies.get(session_id)
        if entry is None:
            raise KycSessionNotFoundError
        return entry

    # ---- Webhooks

    def sign_webhook(self, body: bytes) -> str:
        """Tests: the signature the provider would send with this body."""
        return hmac_sha256_hex(self._webhook_secret, body)

    def verify_webhook_signature(self, body: bytes, signature: str | None) -> bool:
        if signature is None:
            return False
        return signature_matches(self.sign_webhook(body), signature)
