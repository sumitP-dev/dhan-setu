from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.common.exceptions import ForbiddenError, UnauthorizedError
from app.core.config import AppSettings
from app.core.database import DbSession
from app.core.rate_limit import AttemptLimiter, Cooldown
from app.core.redis import RedisClient
from app.core.security import BEARER_CHALLENGE
from app.modules.admin_auth.repository import AdminSessionRepository
from app.modules.admin_auth.service import AdminAuthService
from app.modules.auth.dependencies import UserRepositoryDep, bearer_scheme
from app.modules.auth.otp import OtpService
from app.modules.auth.repository import OtpRepository
from app.modules.notifications.dependencies import SmsSenderDep
from app.modules.users.models import User

# The portal sends this on every cookie request. A page on another site can't add a
# custom header without a CORS preflight, which only the portal's origins pass.
ADMIN_CLIENT_HEADER = "X-Admin-Client"
ADMIN_CLIENT_VALUE = "dhan-setu-admin"


def get_admin_auth_service(
    db: DbSession,
    users: UserRepositoryDep,
    settings: AppSettings,
    redis: RedisClient,
    sms: SmsSenderDep,
) -> AdminAuthService:
    lockout = timedelta(minutes=settings.login_lockout_minutes)
    return AdminAuthService(
        session=db,
        users=users,
        sessions=AdminSessionRepository(db),
        otp=OtpService(db, OtpRepository(db), sms, settings),
        settings=settings,
        login_limiter=AttemptLimiter(
            redis, namespace="admin_login", max_attempts=settings.login_max_attempts, window=lockout
        ),
        otp_cooldown=Cooldown(
            redis,
            namespace="otp_send",
            period=timedelta(seconds=settings.otp_resend_cooldown_seconds),
        ),
    )


AdminAuthServiceDep = Annotated[AdminAuthService, Depends(get_admin_auth_service)]


def require_admin_client(
    request: Request,
    settings: AppSettings,
    client: Annotated[
        str | None, Header(alias=ADMIN_CLIENT_HEADER, include_in_schema=False)
    ] = None,
) -> None:
    """Guards the cookie endpoints against cross-site requests (CSRF)."""
    origin = request.headers.get("origin")
    if client != ADMIN_CLIENT_VALUE or (
        origin is not None and origin not in settings.admin_origins
    ):
        raise ForbiddenError("Request not allowed from here", code="origin_not_allowed")


async def get_current_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AdminAuthServiceDep,
) -> User:
    """An admin portal token (not a customer-app token) whose session is still active,
    for an account that is still an active admin."""
    if credentials is None:
        raise UnauthorizedError(headers=BEARER_CHALLENGE)
    user, _ = await service.authenticate(credentials.credentials)
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]
