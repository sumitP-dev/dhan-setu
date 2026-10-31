from typing import Literal

from pydantic import Field

from app.common.enums import OtpPurpose
from app.common.schemas import RequestModel, ResponseModel
from app.common.validators import (
    ExistingPassword,
    FullName,
    IndianMobileNumber,
    LoginIdentifier,
    NewPassword,
    NormalizedEmail,
    OtpCodeStr,
)
from app.modules.users.schemas import UserRead

# The OTP flows a client may start. reset_password is reserved for a later phase.
ClientOtpPurpose = Literal[OtpPurpose.VERIFY_PHONE, OtpPurpose.LOGIN]


class RegisterRequest(RequestModel):
    email: NormalizedEmail
    phone: IndianMobileNumber = Field(examples=["9876543210"])
    full_name: FullName
    password: NewPassword


class LoginRequest(RequestModel):
    identifier: LoginIdentifier = Field(
        description="Email address or 10-digit mobile number.", examples=["asha@example.com"]
    )
    password: ExistingPassword


class OtpSendRequest(RequestModel):
    phone: IndianMobileNumber = Field(examples=["9876543210"])
    purpose: ClientOtpPurpose


class OtpVerifyRequest(RequestModel):
    phone: IndianMobileNumber = Field(examples=["9876543210"])
    purpose: ClientOtpPurpose
    code: OtpCodeStr = Field(examples=["123456"])


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


class OtpSentResponse(ResponseModel):
    message: str
    expires_in: int = Field(description="Seconds until the code expires.")
    resend_after: int = Field(description="Seconds before another code can be requested.")
    dev_otp: str | None = Field(
        default=None, description="Only when the server runs with DEBUG=true (never in production)."
    )


class RegisterResponse(ResponseModel):
    """Registration doesn't sign the user in: they must verify the OTP first."""

    user: UserRead
    otp: OtpSentResponse
