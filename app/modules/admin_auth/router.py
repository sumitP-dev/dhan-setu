from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.common.schemas import error_responses
from app.core.config import AppSettings, Settings
from app.core.database import utcnow
from app.modules.admin_auth.dependencies import (
    AdminAuthServiceDep,
    CurrentAdmin,
    require_admin_client,
)
from app.modules.admin_auth.schemas import (
    AdminChallenge,
    AdminLoginRequest,
    AdminRead,
    AdminSessionResponse,
    AdminVerifyRequest,
)
from app.modules.admin_auth.service import SignedIn
from app.modules.auth.dependencies import DeviceInfo

REFRESH_COOKIE = "ds_admin_refresh"
# The __Host- prefix makes browsers refuse the cookie unless it is Secure, for this
# exact host and path "/", so no other subdomain can set or replace it.
SECURE_REFRESH_COOKIE = f"__Host-{REFRESH_COOKIE}"

router = APIRouter(
    prefix="/admin/auth",
    tags=["admin: auth"],
    dependencies=[Depends(require_admin_client)],
    responses=error_responses(status.HTTP_403_FORBIDDEN, status.HTTP_422_UNPROCESSABLE_CONTENT),
)


def cookie_name(settings: Settings) -> str:
    return SECURE_REFRESH_COOKIE if settings.admin_cookie_secure else REFRESH_COOKIE


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _set_session_cookie(response: Response, settings: Settings, signed_in: SignedIn) -> None:
    response.set_cookie(
        cookie_name(settings),
        signed_in.refresh_token,
        max_age=max(int((signed_in.expires_at - utcnow()).total_seconds()), 0),
        path="/",
        secure=settings.admin_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=settings.admin_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _refresh_cookie(request: Request, settings: AppSettings) -> str | None:
    return request.cookies.get(cookie_name(settings))


RefreshCookie = Annotated[str | None, Depends(_refresh_cookie)]


@router.post(
    "/login",
    response_model_exclude_none=True,
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_429_TOO_MANY_REQUESTS),
)
async def admin_login(
    body: AdminLoginRequest, response: Response, service: AdminAuthServiceDep
) -> AdminChallenge:
    """Step 1: email and password of an admin account. Texts a code to its phone."""
    _no_store(response)
    return await service.login(body)


@router.post(
    "/verify",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED, status.HTTP_429_TOO_MANY_REQUESTS
    ),
)
async def admin_verify(
    body: AdminVerifyRequest,
    request: Request,
    response: Response,
    service: AdminAuthServiceDep,
    settings: AppSettings,
    user_agent: DeviceInfo,
) -> AdminSessionResponse:
    """Step 2: the code from the SMS. Returns an access token and sets the session cookie."""
    signed_in = await service.verify(
        body,
        ip_address=request.client.host if request.client else None,
        user_agent=user_agent,
    )
    _no_store(response)
    _set_session_cookie(response, settings, signed_in)
    return signed_in.response


@router.post(
    "/refresh",
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_409_CONFLICT),
)
async def admin_refresh(
    response: Response,
    service: AdminAuthServiceDep,
    settings: AppSettings,
    refresh_token: RefreshCookie,
) -> AdminSessionResponse:
    """A new access token from the session cookie (which is replaced too). 409
    refresh_in_progress: another tab just refreshed; try again."""
    signed_in = await service.refresh(refresh_token)
    _no_store(response)
    _set_session_cookie(response, settings, signed_in)
    return signed_in.response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def admin_logout(
    response: Response,
    service: AdminAuthServiceDep,
    settings: AppSettings,
    refresh_token: RefreshCookie,
) -> None:
    """Ends the session and clears the cookie."""
    await service.logout(refresh_token)
    _clear_session_cookie(response, settings)


@router.get("/me", responses=error_responses(status.HTTP_401_UNAUTHORIZED))
async def admin_me(admin: CurrentAdmin, response: Response) -> AdminRead:
    _no_store(response)
    return AdminRead.model_validate(admin)
