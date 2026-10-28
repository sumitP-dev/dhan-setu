from datetime import timedelta
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.exceptions import ForbiddenError, UnauthorizedError
from app.core.config import AppSettings
from app.core.database import DbSession
from app.core.rate_limit import AttemptLimiter
from app.core.redis import RedisClient
from app.core.security import BEARER_CHALLENGE, decode_access_token
from app.modules.auth.repository import RefreshTokenRepository
from app.modules.auth.service import AuthService
from app.modules.users.dependencies import get_user_repository
from app.modules.users.models import User
from app.modules.users.repository import UserRepository

# auto_error=False: we raise our own 401 so the error shape stays consistent.
bearer_scheme = HTTPBearer(auto_error=False, description="Access token from /auth/login")

UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]


def get_auth_service(
    db: DbSession, users: UserRepositoryDep, settings: AppSettings, redis: RedisClient
) -> AuthService:
    return AuthService(
        session=db,
        users=users,
        refresh_tokens=RefreshTokenRepository(db),
        settings=settings,
        login_limiter=AttemptLimiter(
            redis,
            namespace="login",
            max_attempts=settings.login_max_attempts,
            window=timedelta(minutes=settings.login_lockout_minutes),
        ),
        password_change_limiter=AttemptLimiter(
            redis,
            namespace="password_change",
            max_attempts=settings.login_max_attempts,
            window=timedelta(minutes=settings.login_lockout_minutes),
        ),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


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
    if not user.is_active:
        raise ForbiddenError("This account is disabled", code="account_disabled")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
