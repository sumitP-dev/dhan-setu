from fastapi import APIRouter, status

from app.common.schemas import error_responses
from app.modules.auth.dependencies import AuthServiceDep, CurrentUser, DeviceInfo
from app.modules.auth.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    OtpSendRequest,
    OtpSentResponse,
    OtpVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenPair,
)
from app.modules.auth.service import AuthResult
from app.modules.users.schemas import UserRead

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT),
)


def _auth_response(result: AuthResult) -> AuthResponse:
    return AuthResponse(user=UserRead.model_validate(result.user), tokens=result.tokens)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def register(body: RegisterRequest, service: AuthServiceDep) -> RegisterResponse:
    """Creates the account (pending) and texts a verify_phone code. Call /auth/otp/verify
    with that code to activate the account and get tokens."""
    result = await service.register(body)
    return RegisterResponse(user=UserRead.model_validate(result.user), otp=result.otp)


@router.post(
    "/otp/send",
    response_model_exclude_none=True,
    responses=error_responses(status.HTTP_429_TOO_MANY_REQUESTS),
)
async def send_otp(body: OtpSendRequest, service: AuthServiceDep) -> OtpSentResponse:
    """Texts a code. verify_phone: for accounts not verified yet. login: for active
    accounts. The response is the same either way, so it never reveals whether the
    number is registered. One code per number per 30 seconds."""
    return await service.send_otp(body)


@router.post(
    "/otp/verify",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def verify_otp(
    body: OtpVerifyRequest, service: AuthServiceDep, device_info: DeviceInfo
) -> AuthResponse:
    """verify_phone activates the account. Both purposes return the user and tokens."""
    return _auth_response(await service.verify_otp(body, device_info=device_info))


@router.post(
    "/login",
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def login(
    body: LoginRequest, service: AuthServiceDep, device_info: DeviceInfo
) -> AuthResponse:
    """403 phone_not_verified: send a verify_phone OTP and verify it, then log in."""
    return _auth_response(await service.login(body, device_info=device_info))


@router.post(
    "/refresh",
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)
async def refresh(
    body: RefreshRequest, service: AuthServiceDep, device_info: DeviceInfo
) -> TokenPair:
    """Swap a refresh token for a new pair. The old refresh token stops working."""
    return await service.refresh(body.refresh_token, device_info=device_info)


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
    body: ChangePasswordRequest, user: CurrentUser, service: AuthServiceDep, device_info: DeviceInfo
) -> TokenPair:
    """Signs out all other devices and returns new tokens for this one."""
    return await service.change_password(user, body, device_info=device_info)
