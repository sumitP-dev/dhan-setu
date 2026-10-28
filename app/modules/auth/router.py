from fastapi import APIRouter, status

from app.common.schemas import error_responses
from app.modules.auth.dependencies import AuthServiceDep, CurrentUser
from app.modules.auth.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from app.modules.users.schemas import UserRead

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT),
)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def register(body: RegisterRequest, service: AuthServiceDep) -> AuthResponse:
    result = await service.register(body)
    return AuthResponse(user=UserRead.model_validate(result.user), tokens=result.tokens)


@router.post(
    "/login",
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def login(body: LoginRequest, service: AuthServiceDep) -> AuthResponse:
    result = await service.login(body)
    return AuthResponse(user=UserRead.model_validate(result.user), tokens=result.tokens)


@router.post(
    "/refresh",
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)
async def refresh(body: RefreshRequest, service: AuthServiceDep) -> TokenPair:
    """Swap a refresh token for a new pair. The old refresh token stops working."""
    return await service.refresh(body.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest, service: AuthServiceDep) -> None:
    await service.logout(body.refresh_token)


@router.post(
    "/change-password",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def change_password(
    body: ChangePasswordRequest, user: CurrentUser, service: AuthServiceDep
) -> TokenPair:
    """Signs out all other devices and returns new tokens for this one."""
    return await service.change_password(user, body)
