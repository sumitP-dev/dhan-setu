import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BadRequestError, ConflictError, ForbiddenError, UnauthorizedError
from app.core.config import Settings
from app.core.database import utcnow
from app.core.rate_limit import AttemptLimiter
from app.core.security import (
    burn_password_check,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.modules.auth.models import RefreshToken
from app.modules.auth.repository import RefreshTokenRepository
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    TokenPair,
)
from app.modules.users.models import User
from app.modules.users.repository import UserRepository


@dataclass(frozen=True, slots=True)
class AuthResult:
    user: User
    tokens: TokenPair


def _invalid_credentials() -> UnauthorizedError:
    # Same error for unknown email and wrong password, so it can't reveal which emails exist.
    return UnauthorizedError("Invalid email or password", code="invalid_credentials")


def _invalid_refresh_token() -> UnauthorizedError:
    return UnauthorizedError("Invalid or expired refresh token", code="invalid_refresh_token")


def _account_disabled() -> ForbiddenError:
    return ForbiddenError("This account is disabled", code="account_disabled")


class AuthService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        settings: Settings,
        login_limiter: AttemptLimiter,
        password_change_limiter: AttemptLimiter,
    ) -> None:
        self._session = session
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._settings = settings
        self._login_limiter = login_limiter
        self._password_change_limiter = password_change_limiter

    async def register(self, data: RegisterRequest) -> AuthResult:
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

        return AuthResult(user=user, tokens=await self._issue_tokens(user.id))

    async def login(self, data: LoginRequest) -> AuthResult:
        await self._login_limiter.ensure_allowed(data.email)

        user = await self._users.get_by_email(data.email)
        if user is None:
            await burn_password_check(data.password, rounds=self._settings.bcrypt_rounds)
            await self._login_limiter.record_failure(data.email)
            raise _invalid_credentials()
        if not await verify_password(data.password, user.password_hash):
            await self._login_limiter.record_failure(data.email)
            raise _invalid_credentials()

        await self._login_limiter.reset(data.email)
        # Checked only after the password, so it doesn't reveal that the account exists.
        if not user.is_active:
            raise _account_disabled()
        return AuthResult(user=user, tokens=await self._issue_tokens(user.id))

    async def refresh(self, refresh_token: str) -> TokenPair:
        now = utcnow()
        token_hash = hash_refresh_token(refresh_token)

        consumed = await self._refresh_tokens.consume(token_hash, now=now)
        if consumed is None:
            await self._handle_unusable_refresh_token(token_hash)
            raise _invalid_refresh_token()

        user = await self._users.get_by_id(consumed.user_id)
        if user is None:
            raise _invalid_refresh_token()
        if not user.is_active:
            raise _account_disabled()
        return await self._issue_tokens(user.id, family_id=consumed.family_id)

    async def logout(self, refresh_token: str) -> None:
        """Ends the session on this device. Unknown or already-revoked tokens are ignored,
        so logout is safe to retry."""
        token = await self._refresh_tokens.get_by_hash(hash_refresh_token(refresh_token))
        if token is not None:
            await self._refresh_tokens.revoke_family(token.family_id, now=utcnow())

    async def change_password(self, user: User, data: ChangePasswordRequest) -> TokenPair:
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
        return await self._issue_tokens(user.id)

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
        self, user_id: uuid.UUID, *, family_id: uuid.UUID | None = None
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
            )
        )
        return TokenPair(
            access_token=create_access_token(
                user_id, secret=self._settings.jwt_secret_key, ttl=access_ttl, now=now
            ),
            refresh_token=refresh_token,
            expires_in=int(access_ttl.total_seconds()),
        )
