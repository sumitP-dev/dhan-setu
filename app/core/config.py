from functools import lru_cache
from typing import Annotated, Literal

from fastapi import Depends, Request
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_JWT_SECRET_LENGTH = 32
MIN_WEBHOOK_SECRET_LENGTH = 16
RAZORPAY_TEST_KEY_PREFIX = "rzp_test_"
RAZORPAY_LIVE_KEY_PREFIX = "rzp_live_"

Environment = Literal["local", "test", "staging", "production"]
# "fake" settles payments in memory with no real money. Refused in production.
PaymentGatewayName = Literal["razorpay", "fake"]
# "none": no provider set up yet; KYC is unavailable and nobody can be verified.
KycProviderName = Literal["fake", "none"]


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

    # Payments
    payment_gateway: PaymentGatewayName = "fake"
    # The key id is public (the app sends it to Razorpay checkout); the secrets are not.
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None
    razorpay_timeout_seconds: float = Field(default=10, gt=0, le=30)

    # KYC (Aadhaar and PAN through DigiLocker, plus a selfie). The fake provider
    # verifies anyone, so it is refused in production.
    kyc_provider: KycProviderName = "fake"

    # Cards. Each new card is checked with a ₹1 charge that is refunded straight away.
    # Small test charges are how stolen cards get tried, so new checks are capped per user.
    card_verifications_per_day: int = Field(default=5, gt=0, le=50)
    max_cards_per_user: int = Field(default=10, gt=0, le=50)

    # Payments. Amounts are integer paise (₹1 = 100).
    max_payment_amount_paise: int = Field(default=1_00_000_00, ge=100)  # ₹1,00,000
    # Unpaid payments become failed after this long.
    payment_expiry_minutes: int = Field(default=30, gt=0)
    # Polling a payment checks with the gateway at most this often.
    payment_sync_interval_seconds: int = Field(default=5, gt=0)

    @field_validator("jwt_secret_key")
    @classmethod
    def _jwt_secret_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_JWT_SECRET_LENGTH:
            msg = f"must be at least {MIN_JWT_SECRET_LENGTH} characters"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _kyc_provider_is_safe(self) -> "Settings":
        if self.kyc_provider == "fake" and self.environment == "production":
            msg = "KYC_PROVIDER=fake is not allowed in production"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _payment_gateway_is_safe(self) -> "Settings":
        """Refuse to start with a payment setup that could lose or fake money."""
        is_production = self.environment == "production"
        if self.payment_gateway == "fake":
            if is_production:
                msg = "PAYMENT_GATEWAY=fake is not allowed in production"
                raise ValueError(msg)
            return self

        key_id = self.razorpay_key_id or ""
        key_secret = self.razorpay_key_secret.get_secret_value() if self.razorpay_key_secret else ""
        webhook_secret = (
            self.razorpay_webhook_secret.get_secret_value() if self.razorpay_webhook_secret else ""
        )
        if not key_id or not key_secret or not webhook_secret:
            msg = (
                "PAYMENT_GATEWAY=razorpay needs RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET "
                "and RAZORPAY_WEBHOOK_SECRET"
            )
            raise ValueError(msg)
        if len(webhook_secret) < MIN_WEBHOOK_SECRET_LENGTH:
            msg = f"RAZORPAY_WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters"
            raise ValueError(msg)
        # Live keys move real money: only production may use them, and production must.
        expected_prefix = RAZORPAY_LIVE_KEY_PREFIX if is_production else RAZORPAY_TEST_KEY_PREFIX
        if not key_id.startswith(expected_prefix):
            msg = f"RAZORPAY_KEY_ID must start with {expected_prefix} in {self.environment}"
            raise ValueError(msg)
        return self

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
