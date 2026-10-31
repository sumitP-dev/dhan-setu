import contextlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import OtpPurpose, UserStatus
from app.common.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    TooManyRequestsError,
    UnauthorizedError,
)
from app.common.validators import is_phone_identifier
from app.core.config import Settings
from app.core.database import utcnow
from app.core.rate_limit import AttemptLimiter, Cooldown
from app.core.security import (
    burn_password_check,
    create_access_token,
    generate_otp,
    generate_refresh_token,
    hash_otp,
    hash_password,
    hash_refresh_token,
    otp_matches,
    verify_password,
)
from app.modules.auth.models import MAX_DEVICE_INFO_LENGTH, OtpCode, RefreshToken
from app.modules.auth.repository import OtpRepository, RefreshTokenRepository
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    OtpSendRequest,
    OtpSentResponse,
    OtpVerifyRequest,
    RegisterRequest,
    TokenPair,
)
from app.modules.notifications.service import SmsSender
from app.modules.users.models import User
from app.modules.users.repository import UserRepository

# Which account status may receive each kind of OTP.
_OTP_ELIGIBLE_STATUS = {
    OtpPurpose.VERIFY_PHONE: UserStatus.PENDING_VERIFICATION,
    OtpPurpose.LOGIN: UserStatus.ACTIVE,
}


@dataclass(frozen=True, slots=True)
class AuthResult:
    user: User
    tokens: TokenPair


@dataclass(frozen=True, slots=True)
class RegisterResult:
    user: User
    otp: OtpSentResponse


def _invalid_credentials() -> UnauthorizedError:
    # Same error for unknown account and wrong password, so it can't reveal who is registered.
    return UnauthorizedError("Invalid email/mobile or password", code="invalid_credentials")


def _invalid_refresh_token() -> UnauthorizedError:
    return UnauthorizedError("Invalid or expired refresh token", code="invalid_refresh_token")


def _invalid_otp() -> BadRequestError:
    return BadRequestError("Invalid or expired code", code="invalid_otp")


def account_disabled() -> ForbiddenError:
    return ForbiddenError("This account is disabled", code="account_disabled")


def phone_not_verified() -> ForbiddenError:
    return ForbiddenError("Verify your mobile number to continue", code="phone_not_verified")


def ensure_can_sign_in(user: User) -> None:
    if user.status == UserStatus.BLOCKED:
        raise account_disabled()
    if user.status == UserStatus.PENDING_VERIFICATION:
        raise phone_not_verified()


class AuthService:
    def __init__(  # noqa: PLR0913  (all keyword-only dependencies)
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        otps: OtpRepository,
        sms: SmsSender,
        settings: Settings,
        login_limiter: AttemptLimiter,
        password_change_limiter: AttemptLimiter,
        otp_cooldown: Cooldown,
    ) -> None:
        self._session = session
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._otps = otps
        self._sms = sms
        self._settings = settings
        self._login_limiter = login_limiter
        self._password_change_limiter = password_change_limiter
        self._otp_cooldown = otp_cooldown

    # ---- Registration and OTP

    async def register(self, data: RegisterRequest) -> RegisterResult:
        """Creates a pending account and texts a verify_phone OTP. No tokens until verified."""
        if await self._users.email_exists(data.email):
            raise ConflictError("This email is already registered", code="email_taken")
        if await self._users.phone_exists(data.phone):
            raise ConflictError("This mobile number is already registered", code="phone_taken")

        user = User(
            email=data.email,
            phone=data.phone,
            full_name=data.full_name,
            password_hash=await hash_password(data.password, rounds=self._settings.bcrypt_rounds),
        )
        try:
            await self._users.add(user)
        except IntegrityError as exc:
            # Lost a race with a concurrent sign-up using the same email or phone.
            raise ConflictError(
                "An account with these details already exists", code="account_exists"
            ) from exc

        # Start the resend timer. If someone already requested a code for this number,
        # registration still goes ahead (the account didn't exist then).
        with contextlib.suppress(TooManyRequestsError):
            await self._otp_cooldown.start(self._cooldown_key(user.phone, OtpPurpose.VERIFY_PHONE))
        code = await self._issue_otp(user.phone, OtpPurpose.VERIFY_PHONE)
        return RegisterResult(user=user, otp=self._otp_sent(code))

    async def send_otp(self, data: OtpSendRequest) -> OtpSentResponse:
        """Always answers the same way, whether or not the number is registered, so this
        endpoint can't be used to discover which numbers have accounts. A code is only
        actually sent when the account is in the right state for the purpose."""
        await self._otp_cooldown.start(self._cooldown_key(data.phone, data.purpose))

        user = await self._users.get_by_phone(data.phone)
        if user is None or user.status != _OTP_ELIGIBLE_STATUS[data.purpose]:
            return self._otp_sent(None)
        code = await self._issue_otp(data.phone, data.purpose)
        return self._otp_sent(code)

    async def verify_otp(self, data: OtpVerifyRequest, *, device_info: str | None) -> AuthResult:
        """verify_phone activates the account; both purposes sign the user in."""
        now = utcnow()
        otp = await self._otps.get_active(data.phone, data.purpose, now=now)
        if otp is None:
            raise _invalid_otp()

        if not await self._otps.record_attempt(
            otp.id, max_attempts=self._settings.otp_max_attempts
        ):
            raise TooManyRequestsError(
                "Too many wrong codes. Request a new one.", code="otp_attempts_exceeded"
            )
        secret = self._settings.jwt_secret_key
        if not otp_matches(
            data.code, otp.code_hash, phone=data.phone, purpose=data.purpose, secret=secret
        ):
            # Commit now: the request is about to fail, and a failing request is rolled
            # back, which would undo the attempt count.
            await self._session.commit()
            raise _invalid_otp()
        if not await self._otps.consume(otp.id, now=now):
            raise _invalid_otp()  # used by a parallel request

        user = await self._users.get_by_phone(data.phone)
        if user is None:
            raise _invalid_otp()
        if data.purpose == OtpPurpose.VERIFY_PHONE and (
            user.status == UserStatus.PENDING_VERIFICATION
        ):
            user.status = UserStatus.ACTIVE
        ensure_can_sign_in(user)
        return await self._sign_in(user, device_info=device_info)

    # ---- Password login and sessions

    async def login(self, data: LoginRequest, *, device_info: str | None) -> AuthResult:
        if is_phone_identifier(data.identifier):
            user = await self._users.get_by_phone(data.identifier)
        else:
            user = await self._users.get_by_email(data.identifier)

        # Count failures per account, so switching between email and phone doesn't give
        # extra guesses. Unknown identifiers are counted too, so they look the same.
        limiter_key = str(user.id) if user else data.identifier
        await self._login_limiter.ensure_allowed(limiter_key)

        if user is None:
            await burn_password_check(data.password, rounds=self._settings.bcrypt_rounds)
            await self._login_limiter.record_failure(limiter_key)
            raise _invalid_credentials()
        if not await verify_password(data.password, user.password_hash):
            await self._login_limiter.record_failure(limiter_key)
            raise _invalid_credentials()

        await self._login_limiter.reset(limiter_key)
        # Checked only after the password, so it doesn't reveal that the account exists.
        ensure_can_sign_in(user)
        return await self._sign_in(user, device_info=device_info)

    async def refresh(self, refresh_token: str, *, device_info: str | None) -> TokenPair:
        now = utcnow()
        token_hash = hash_refresh_token(refresh_token)

        consumed = await self._refresh_tokens.consume(token_hash, now=now)
        if consumed is None:
            await self._handle_unusable_refresh_token(token_hash)
            raise _invalid_refresh_token()

        user = await self._users.get_by_id(consumed.user_id)
        if user is None:
            raise _invalid_refresh_token()
        ensure_can_sign_in(user)
        return await self._issue_tokens(
            user.id, family_id=consumed.family_id, device_info=device_info
        )

    async def logout(self, refresh_token: str) -> None:
        """Ends the session on this device. Unknown or already-revoked tokens are ignored,
        so logout is safe to retry."""
        token = await self._refresh_tokens.get_by_hash(hash_refresh_token(refresh_token))
        if token is not None:
            await self._refresh_tokens.revoke_family(token.family_id, now=utcnow())

    async def change_password(
        self, user: User, data: ChangePasswordRequest, *, device_info: str | None
    ) -> TokenPair:
        """Signs out every other device and returns fresh tokens for this one."""
        limiter_key = str(user.id)
        await self._password_change_limiter.ensure_allowed(limiter_key)

        if not await verify_password(data.current_password, user.password_hash):
            await self._password_change_limiter.record_failure(limiter_key)
            raise BadRequestError("Current password is incorrect", code="wrong_password")
        if data.new_password == data.current_password:
            raise BadRequestError(
                "New password must be different from the current one", code="password_unchanged"
            )

        await self._password_change_limiter.reset(limiter_key)
        user.password_hash = await hash_password(
            data.new_password, rounds=self._settings.bcrypt_rounds
        )
        await self._users.save(user)
        await self._refresh_tokens.revoke_all_for_user(user.id, now=utcnow())
        return await self._issue_tokens(user.id, device_info=device_info)

    # ---- Helpers

    @staticmethod
    def _cooldown_key(phone: str, purpose: OtpPurpose) -> str:
        return f"{purpose}:{phone}"

    async def _issue_otp(self, phone: str, purpose: OtpPurpose) -> str:
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

    def _otp_sent(self, code: str | None) -> OtpSentResponse:
        return OtpSentResponse(
            message="If this number can receive a code, it has been sent.",
            expires_in=self._settings.otp_ttl_minutes * 60,
            resend_after=self._settings.otp_resend_cooldown_seconds,
            dev_otp=code if self._settings.expose_dev_otp else None,
        )

    async def _sign_in(self, user: User, *, device_info: str | None) -> AuthResult:
        user.last_login_at = utcnow()
        await self._users.save(user)
        return AuthResult(
            user=user, tokens=await self._issue_tokens(user.id, device_info=device_info)
        )

    async def _handle_unusable_refresh_token(self, token_hash: str) -> None:
        """A refresh token that exists but was already used means it was probably copied
        by someone else. Revoke the whole family so neither copy works any more."""
        token = await self._refresh_tokens.get_by_hash(token_hash)
        if token is None or token.revoked_at is None:
            return  # unknown or merely expired
        await self._refresh_tokens.revoke_family(token.family_id, now=utcnow())
        # Commit now: the request is about to fail, and a failing request is rolled back.
        await self._session.commit()

    async def _issue_tokens(
        self,
        user_id: uuid.UUID,
        *,
        family_id: uuid.UUID | None = None,
        device_info: str | None = None,
    ) -> TokenPair:
        now = utcnow()
        access_ttl = timedelta(minutes=self._settings.access_token_ttl_minutes)
        refresh_token = generate_refresh_token()

        await self._refresh_tokens.add(
            RefreshToken(
                user_id=user_id,
                family_id=family_id or uuid.uuid4(),
                token_hash=hash_refresh_token(refresh_token),
                expires_at=now + timedelta(days=self._settings.refresh_token_ttl_days),
                device_info=device_info[:MAX_DEVICE_INFO_LENGTH] if device_info else None,
            )
        )
        return TokenPair(
            access_token=create_access_token(
                user_id, secret=self._settings.jwt_secret_key, ttl=access_ttl, now=now
            ),
            refresh_token=refresh_token,
            expires_in=int(access_ttl.total_seconds()),
        )
