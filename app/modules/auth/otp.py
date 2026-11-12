"""Issuing and checking one-time codes, shared by customer and admin sign-in."""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import OtpPurpose
from app.common.exceptions import BadRequestError, TooManyRequestsError
from app.core.config import Settings
from app.core.database import utcnow
from app.core.security import generate_otp, hash_otp, otp_matches
from app.modules.auth.models import OtpCode
from app.modules.auth.repository import OtpRepository
from app.modules.notifications.service import SmsSender


def invalid_otp() -> BadRequestError:
    return BadRequestError("Invalid or expired code", code="invalid_otp")


class OtpService:
    def __init__(
        self, session: AsyncSession, otps: OtpRepository, sms: SmsSender, settings: Settings
    ) -> None:
        self._session = session
        self._otps = otps
        self._sms = sms
        self._settings = settings

    async def issue(self, phone: str, purpose: OtpPurpose) -> str:
        """Texts a new code; older codes for this phone and purpose stop working."""
        now = utcnow()
        code = generate_otp()
        await self._otps.invalidate_active(phone, purpose, now=now)
        await self._otps.add(
            OtpCode(
                phone=phone,
                purpose=purpose,
                code_hash=hash_otp(
                    code, phone=phone, purpose=purpose, secret=self._settings.jwt_secret_key
                ),
                expires_at=now + timedelta(minutes=self._settings.otp_ttl_minutes),
            )
        )
        await self._sms.send_otp(phone, code, purpose)
        return code

    async def check(self, phone: str, purpose: OtpPurpose, code: str) -> None:
        """Uses up the code if it is right. Raises invalid_otp or otp_attempts_exceeded."""
        now = utcnow()
        otp = await self._otps.get_active(phone, purpose, now=now)
        if otp is None:
            raise invalid_otp()
        if not await self._otps.record_attempt(
            otp.id, max_attempts=self._settings.otp_max_attempts
        ):
            raise TooManyRequestsError(
                "Too many wrong codes. Request a new one.", code="otp_attempts_exceeded"
            )
        secret = self._settings.jwt_secret_key
        if not otp_matches(code, otp.code_hash, phone=phone, purpose=purpose, secret=secret):
            # Commit now: the request is about to fail, and a failing request is rolled
            # back, which would undo the attempt count.
            await self._session.commit()
            raise invalid_otp()
        if not await self._otps.consume(otp.id, now=now):
            raise invalid_otp()  # used by a parallel request
