import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import UserStatus
from app.modules.auth.models import RefreshToken
from app.modules.users.models import User
from tests.helpers import FakeRedis, register
from tests.helpers import login as _login


@pytest.fixture(autouse=True)
async def _registered(client: AsyncClient) -> None:
    await register(client)


async def test_login_success(client: AsyncClient) -> None:
    response = await _login(client, "ASHA@example.com")

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "asha@example.com"
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]


async def test_wrong_password_and_unknown_email_look_the_same(client: AsyncClient) -> None:
    wrong_password = await _login(client, password="Wrong1234")
    unknown_email = await _login(client, "nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert (
        wrong_password.json()
        == unknown_email.json()
        == {"error": {"code": "invalid_credentials", "message": "Invalid email/mobile or password"}}
    )


async def test_locks_out_after_too_many_failures(client: AsyncClient) -> None:
    for _ in range(5):
        assert (await _login(client, password="Wrong1234")).status_code == 401

    response = await _login(client)  # even the right password is blocked now

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_attempts"
    assert int(response.headers["retry-after"]) > 0


async def test_lockout_ends_after_window(client: AsyncClient, fake_redis: FakeRedis) -> None:
    for _ in range(5):
        await _login(client, password="Wrong1234")

    fake_redis.expire_now("attempts:login:")

    assert (await _login(client)).status_code == 200


async def test_successful_login_resets_failure_count(client: AsyncClient) -> None:
    for _ in range(4):
        await _login(client, password="Wrong1234")
    assert (await _login(client)).status_code == 200

    for _ in range(4):
        await _login(client, password="Wrong1234")

    assert (await _login(client)).status_code == 200


async def test_disabled_account_cannot_log_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(update(User).values(status=UserStatus.BLOCKED))
    await db_session.commit()

    response = await _login(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


async def test_disabled_account_with_wrong_password_reveals_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(update(User).values(status=UserStatus.BLOCKED))
    await db_session.commit()

    response = await _login(client, password="Wrong1234")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_with_phone_in_any_format(client: AsyncClient) -> None:
    for identifier in ("9876543210", "+91 98765 43210", "09876543210"):
        response = await _login(client, identifier)
        assert response.status_code == 200, identifier
        assert response.json()["user"]["phone"] == "+919876543210"


async def test_unknown_phone_looks_like_wrong_password(client: AsyncClient) -> None:
    unknown = await _login(client, "9123456789")
    wrong = await _login(client, "9876543210", password="Wrong1234")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_invalid_identifier_is_rejected(client: AsyncClient) -> None:
    for identifier in ("12345", "not-an-email@", "asha"):
        response = await _login(client, identifier)
        assert response.status_code == 422, identifier
        assert "email or 10-digit" in response.json()["error"]["message"]


async def test_lockout_is_shared_between_email_and_phone(client: AsyncClient) -> None:
    """Alternating email and phone must not give an attacker extra guesses."""
    for identifier in ("asha@example.com", "9876543210") * 2 + ("asha@example.com",):
        await _login(client, identifier, password="Wrong1234")

    response = await _login(client, "9876543210")

    assert response.status_code == 429


async def test_login_records_last_login_and_device(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await client.post(
        "/api/v1/auth/login",
        json={"identifier": "asha@example.com", "password": "Secret123"},
        headers={"User-Agent": "DhanSetu/1.0 (Android 15)"},
    )

    user = await db_session.scalar(select(User))
    devices = (await db_session.scalars(select(RefreshToken.device_info))).all()
    assert user is not None
    assert user.last_login_at is not None
    assert "DhanSetu/1.0 (Android 15)" in devices
