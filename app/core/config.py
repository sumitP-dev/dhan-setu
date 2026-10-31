from functools import lru_cache
from typing import Annotated, Literal

from fastapi import Depends, Request
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_JWT_SECRET_LENGTH = 32

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    """App settings, read from environment variables and the .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Dhan Setu API"
    environment: Environment = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # Browser origins allowed to call the API (e.g. a future web dashboard). The mobile
    # app is not a browser and does not need CORS. Empty list = CORS disabled.
    cors_origins: list[str] = []

    # Required, no defaults: connection URLs can hold credentials, so they come only
    # from the environment. SecretStr keeps them out of logs and reprs.
    database_url: SecretStr
    database_echo: bool = False
    redis_url: SecretStr

    # Auth
    jwt_secret_key: SecretStr
    access_token_ttl_minutes: int = Field(default=15, gt=0)
    refresh_token_ttl_days: int = Field(default=7, gt=0)
    # Each +1 doubles hashing time. 12 is ~0.25s; tests lower it to 4 for speed.
    bcrypt_rounds: int = Field(default=12, ge=4, le=16)
    login_max_attempts: int = Field(default=5, gt=0)
    login_lockout_minutes: int = Field(default=15, gt=0)

    # One-time codes sent by SMS
    otp_ttl_minutes: int = Field(default=5, gt=0)
    otp_max_attempts: int = Field(default=5, gt=0)
    otp_resend_cooldown_seconds: int = Field(default=30, gt=0)

    @field_validator("jwt_secret_key")
    @classmethod
    def _jwt_secret_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_JWT_SECRET_LENGTH:
            msg = f"must be at least {MIN_JWT_SECRET_LENGTH} characters"
            raise ValueError(msg)
        return value

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"

    @property
    def expose_dev_otp(self) -> bool:
        """Return OTPs in API responses so developers don't need a real SMS.
        Never in production, even if DEBUG is switched on there by mistake."""
        return self.debug and self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_app_settings(request: Request) -> Settings:
    """The settings the running app was built with (test apps get test settings)."""
    settings: Settings = request.app.state.settings
    return settings


AppSettings = Annotated[Settings, Depends(get_app_settings)]
