import math
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from app.common.enums import KycStatus, OtpPurpose
from app.core.config import Settings

if TYPE_CHECKING:
    from app.modules.payments.gateway.fake import FakePaymentGateway

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TEST_REDIS_URL = "redis://localhost:6379/15"
TEST_JWT_SECRET = "test-jwt-secret-that-is-long-enough-0123456789"
TEST_PASSWORD = "Secret123"
# 32 fixed bytes, URL-safe base64: test-only KYC encryption key.
TEST_KYC_KEY = "dGVzdC1reWMta2V5LXRlc3Qta3ljLWtleS10ZXN0LWs="


class _IsolatedSettings(Settings):
    # Tests must never read a developer's real .env.
    model_config = SettingsConfigDict(env_file=None)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": SecretStr(TEST_DATABASE_URL),
        "redis_url": SecretStr(TEST_REDIS_URL),
        "jwt_secret_key": SecretStr(TEST_JWT_SECRET),
        "kyc_encryption_key": SecretStr(TEST_KYC_KEY),
        "bcrypt_rounds": 4,  # the minimum; keeps tests fast
        # Responses include dev_otp, which register() below relies on.
        "debug": True,
    } | overrides
    if values["environment"] == "production":
        values.setdefault("admin_origins", ["https://admin.dhansetu.example"])
    else:
        # The test client talks plain http, which never sends Secure cookies.
        values.setdefault("admin_cookie_secure", False)
    return _IsolatedSettings(**values)


class FakeRedis:
    """In-memory stand-in for the Redis client, with key expiry. Add commands as
    features need them."""

    def __init__(self) -> None:
        self.healthy = True
        self._data: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}

    def _purge_if_expired(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and expires_at <= time.monotonic():
            self._data.pop(key, None)
            self._expires_at.pop(key, None)

    def expire_now(self, key_prefix: str) -> None:
        """Test helper: make matching keys expire immediately."""
        for key in list(self._data):
            if key.startswith(key_prefix):
                self._expires_at[key] = 0

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis down")
        return True

    async def get(self, key: str) -> str | None:
        self._purge_if_expired(key)
        return self._data.get(key)

    async def set(
        self, key: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        self._purge_if_expired(key)
        if nx and key in self._data:
            return None
        self._data[key] = value
        self._expires_at.pop(key, None)
        if ex is not None:
            self._expires_at[key] = time.monotonic() + ex
        return True

    async def incr(self, key: str) -> int:
        self._purge_if_expired(key)
        value = int(self._data.get(key, "0")) + 1
        self._data[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int, *, nx: bool = False) -> bool:
        self._purge_if_expired(key)
        if key not in self._data or (nx and key in self._expires_at):
            return False
        self._expires_at[key] = time.monotonic() + seconds
        return True

    async def ttl(self, key: str) -> int:
        self._purge_if_expired(key)
        if key not in self._data:
            return -2
        if key not in self._expires_at:
            return -1
        return math.ceil(self._expires_at[key] - time.monotonic())

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            self._expires_at.pop(key, None)
            removed += self._data.pop(key, None) is not None
        return removed

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class SentSms:
    phone: str
    code: str
    purpose: OtpPurpose


class FakeSmsSender:
    """Records OTPs instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[SentSms] = []

    async def send_otp(self, phone: str, code: str, purpose: OtpPurpose) -> None:
        self.sent.append(SentSms(phone, code, purpose))

    @property
    def last(self) -> SentSms:
        return self.sent[-1]


# ---- API helpers


def user_payload(**overrides: str) -> dict[str, str]:
    return {
        "email": "asha@example.com",
        "phone": "9876543210",
        "full_name": "Asha Verma",
        "password": TEST_PASSWORD,
    } | overrides


async def start_registration(client: AsyncClient, **overrides: str) -> dict[str, Any]:
    """POST /auth/register only: the account stays pending_verification."""
    response = await client.post("/api/v1/auth/register", json=user_payload(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def verify_otp(
    client: AsyncClient, phone: str, code: str, purpose: str = "verify_phone"
) -> Response:
    return await client.post(
        "/api/v1/auth/otp/verify", json={"phone": phone, "purpose": purpose, "code": code}
    )


async def register(client: AsyncClient, **overrides: str) -> dict[str, Any]:
    """Register and verify the phone: returns {user, tokens} for an active account."""
    started = await start_registration(client, **overrides)
    response = await verify_otp(client, started["user"]["phone"], started["otp"]["dev_otp"])
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def login(
    client: AsyncClient, identifier: str = "asha@example.com", password: str = TEST_PASSWORD
) -> Response:
    return await client.post(
        "/api/v1/auth/login", json={"identifier": identifier, "password": password}
    )


async def mark_kyc_verified(app: FastAPI, user_id: str) -> None:
    """Walk the user through the real KYC status changes to "verified"."""
    from app.modules.kyc.repository import KycRepository  # noqa: PLC0415
    from app.modules.kyc.service import KycService  # noqa: PLC0415

    async with app.state.session_factory() as session:
        service = KycService(KycRepository(session))
        for target in (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW, KycStatus.VERIFIED):
            await service.transition(uuid.UUID(user_id), target, reason="test_setup")
        await session.commit()


async def register_kyc_verified(
    client: AsyncClient, app: FastAPI, **overrides: str
) -> dict[str, Any]:
    """register(), then KYC verified: for tests of cards, payments, UPI and bills."""
    body = await register(client, **overrides)
    await mark_kyc_verified(app, body["user"]["id"])
    return body


# What the admin portal sends on its cookie requests.
ADMIN_CLIENT = {"X-Admin-Client": "dhan-setu-admin"}


async def make_admin(app: FastAPI, email: str) -> None:
    from app.cli import set_role  # noqa: PLC0415  (keeps helpers light to import)
    from app.common.enums import UserRole  # noqa: PLC0415

    async with app.state.session_factory() as session:
        await set_role(session, email, UserRole.ADMIN)


async def admin_sign_in(
    client: AsyncClient, email: str, password: str = TEST_PASSWORD
) -> dict[str, Any]:
    """Both steps of admin sign-in. Returns the /admin/auth/verify body; the session
    cookie stays in the client."""
    started = await client.post(
        "/api/v1/admin/auth/login",
        headers=ADMIN_CLIENT,
        json={"email": email, "password": password},
    )
    assert started.status_code == 200, started.text
    response = await client.post(
        "/api/v1/admin/auth/verify",
        headers=ADMIN_CLIENT,
        json={
            "challenge_token": started.json()["challenge_token"],
            "code": started.json()["dev_otp"],
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def register_admin(
    client: AsyncClient, app: FastAPI, **overrides: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """A new active admin, signed in to the portal: (register body, admin headers)."""
    values = {"email": "admin@example.com", "phone": "9000000009", "full_name": "Meera Admin"}
    body = await register(client, **(values | overrides))
    await make_admin(app, body["user"]["email"])
    signed_in = await admin_sign_in(client, body["user"]["email"])
    return body, bearer(signed_in["access_token"])


def bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


async def add_saved_card(
    client: AsyncClient, headers: dict[str, str], gateway: "FakePaymentGateway", **card: Any
) -> dict[str, Any]:
    """Add a card through the ₹1 check (fake checkout). Returns the saved card."""
    started = await client.post(
        "/api/v1/cards/verifications",
        headers=headers | {"Idempotency-Key": new_idempotency_key()},
    )
    assert started.status_code == 201, started.text
    options = started.json()["checkout"]
    result = gateway.complete_checkout(
        options["order_id"], customer_id=options["customer_id"], save_card=True, **card
    )
    response = await client.post(
        f"/api/v1/cards/verifications/{started.json()['id']}/complete",
        headers=headers,
        json={"payment_id": result.payment_id, "signature": result.signature},
    )
    assert response.json()["status"] == "verified", response.text
    saved: dict[str, Any] = response.json()["card"]
    return saved
