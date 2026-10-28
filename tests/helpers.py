import math
import time
from typing import Any

from httpx import AsyncClient
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from app.core.config import Settings

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TEST_REDIS_URL = "redis://localhost:6379/15"
TEST_JWT_SECRET = "test-jwt-secret-that-is-long-enough-0123456789"
TEST_PASSWORD = "Secret123"


class _IsolatedSettings(Settings):
    # Tests must never read a developer's real .env.
    model_config = SettingsConfigDict(env_file=None)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": SecretStr(TEST_DATABASE_URL),
        "redis_url": SecretStr(TEST_REDIS_URL),
        "jwt_secret_key": SecretStr(TEST_JWT_SECRET),
        "bcrypt_rounds": 4,  # the minimum; keeps tests fast
    } | overrides
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


# ---- API helpers


def user_payload(**overrides: str) -> dict[str, str]:
    return {
        "email": "asha@example.com",
        "phone": "9876543210",
        "full_name": "Asha Verma",
        "password": TEST_PASSWORD,
    } | overrides


async def register(client: AsyncClient, **overrides: str) -> dict[str, Any]:
    response = await client.post("/api/v1/auth/register", json=user_payload(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}
