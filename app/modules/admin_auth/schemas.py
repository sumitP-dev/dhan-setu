import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.common.schemas import RequestModel, ResponseModel
from app.common.validators import ExistingPassword, NormalizedEmail, OtpCodeStr


class AdminLoginRequest(RequestModel):
    email: NormalizedEmail
    password: ExistingPassword


class AdminChallenge(ResponseModel):
    """The password was right; an OTP was texted to the admin's phone."""

    challenge_token: str = Field(description="Send back with the code to /admin/auth/verify.")
    phone_hint: str = Field(examples=["+91 XXXXXX3210"])
    expires_in: int = Field(description="Seconds until the code and this challenge expire.")
    resend_after: int = Field(description="Seconds before signing in again sends a new code.")
    dev_otp: str | None = Field(
        default=None, description="Only when the server runs with DEBUG=true (never in production)."
    )


class AdminVerifyRequest(RequestModel):
    challenge_token: str = Field(min_length=1, max_length=1024)
    code: OtpCodeStr = Field(examples=["123456"])


class AdminRead(ResponseModel):
    id: uuid.UUID
    full_name: str
    email: str


class AdminSessionResponse(ResponseModel):
    """The refresh token is never in the body: it is set as an httpOnly cookie."""

    admin: AdminRead
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth token type, not a secret)
    expires_in: int = Field(description="Seconds until the access token expires.")
    session_expires_at: datetime = Field(description="When this sign-in ends at the latest.")
