from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import User
from tests.helpers import TEST_PASSWORD, FakeRedis, register


async def _login(
    client: AsyncClient, email: str = "asha@example.com", password: str = TEST_PASSWORD
) -> Any:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


@pytest.fixture(autouse=True)
async def _registered(client: AsyncClient) -> None:
    await register(client)


async def test_login_success(client: AsyncClient) -> None:
    response = await _login(client, email="ASHA@example.com")

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "asha@example.com"
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]


async def test_wrong_password_and_unknown_email_look_the_same(client: AsyncClient) -> None:
    wrong_password = await _login(client, password="Wrong1234")
    unknown_email = await _login(client, email="nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert (
        wrong_password.json()
        == unknown_email.json()
        == {"error": {"code": "invalid_credentials", "message": "Invalid email or password"}}
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
    await db_session.execute(update(User).values(is_active=False))
    await db_session.commit()

    response = await _login(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


async def test_disabled_account_with_wrong_password_reveals_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(update(User).values(is_active=False))
    await db_session.commit()

    response = await _login(client, password="Wrong1234")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
