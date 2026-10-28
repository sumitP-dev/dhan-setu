from typing import Literal

from pydantic import Field

from app.common.schemas import RequestModel, ResponseModel
from app.common.validators import (
    ExistingPassword,
    FullName,
    IndianMobileNumber,
    NewPassword,
    NormalizedEmail,
)
from app.modules.users.schemas import UserRead


class RegisterRequest(RequestModel):
    email: NormalizedEmail
    phone: IndianMobileNumber = Field(examples=["9876543210"])
    full_name: FullName
    password: NewPassword


class LoginRequest(RequestModel):
    email: NormalizedEmail
    password: ExistingPassword


class RefreshRequest(RequestModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class LogoutRequest(RefreshRequest):
    pass


class ChangePasswordRequest(RequestModel):
    current_password: ExistingPassword
    new_password: NewPassword


class TokenPair(ResponseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth token type, not a secret)
    expires_in: int = Field(description="Seconds until the access token expires.")


class AuthResponse(ResponseModel):
    user: UserRead
    tokens: TokenPair
