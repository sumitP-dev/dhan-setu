from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.enums import UserRole
from app.common.exceptions import ForbiddenError, UnauthorizedError
from app.core.config import AppSettings
from app.core.database import DbSession
from app.core.rate_limit import AttemptLimiter, Cooldown
from app.core.redis import RedisClient
from app.core.security import BEARER_CHALLENGE, decode_access_token
from app.modules.auth.repository import OtpRepository, RefreshTokenRepository
from app.modules.auth.service import AuthService, ensure_can_sign_in
from app.modules.notifications.dependencies import SmsSenderDep
from app.modules.users.dependencies import get_user_repository
from app.modules.users.models import User
from app.modules.users.repository import UserRepository

# auto_error=False: we raise our own 401 so the error shape stays consistent.
bearer_scheme = HTTPBearer(auto_error=False, description="Access token from /auth/login")

UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]


def get_auth_service(
    db: DbSession,
    users: UserRepositoryDep,
    settings: AppSettings,
    redis: RedisClient,
    sms: SmsSenderDep,
) -> AuthService:
    lockout = timedelta(minutes=settings.login_lockout_minutes)
    return AuthService(
        session=db,
        users=users,
        refresh_tokens=RefreshTokenRepository(db),
        otps=OtpRepository(db),
        sms=sms,
        settings=settings,
        login_limiter=AttemptLimiter(
            redis, namespace="login", max_attempts=settings.login_max_attempts, window=lockout
        ),
        password_change_limiter=AttemptLimiter(
            redis,
            namespace="password_change",
            max_attempts=settings.login_max_attempts,
            window=lockout,
        ),
        otp_cooldown=Cooldown(
            redis,
            namespace="otp_send",
            period=timedelta(seconds=settings.otp_resend_cooldown_seconds),
        ),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_device_info(
    user_agent: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> str | None:
    return user_agent


DeviceInfo = Annotated[str | None, Depends(get_device_info)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    users: UserRepositoryDep,
    settings: AppSettings,
) -> User:
    if credentials is None:
        raise UnauthorizedError(headers=BEARER_CHALLENGE)

    claims = decode_access_token(credentials.credentials, secret=settings.jwt_secret_key)
    user = await users.get_by_id(claims.user_id)
    if user is None:
        raise UnauthorizedError(
            "Invalid access token", code="invalid_token", headers=BEARER_CHALLENGE
        )
    ensure_can_sign_in(user)  # blocked accounts are cut off at once, not when the token expires
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[[User], Awaitable[User]]:
    """Dependency that allows only users with one of these roles (403 otherwise).

    AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
    """
    allowed = frozenset(roles)

    async def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise ForbiddenError("You don't have permission to do this", code="insufficient_role")
        return user

    return dependency
