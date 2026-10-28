from typing import Any

from httpx import AsyncClient, Response

from tests.helpers import TEST_PASSWORD, bearer, register


async def _change(client: AsyncClient, access_token: str, current: str, new: str) -> Response:
    return await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": current, "new_password": new},
        headers=bearer(access_token),
    )


async def _login(client: AsyncClient, password: str) -> Response:
    return await client.post(
        "/api/v1/auth/login", json={"email": "asha@example.com", "password": password}
    )


async def test_change_password(client: AsyncClient) -> None:
    tokens: dict[str, Any] = (await register(client))["tokens"]

    response = await _change(client, tokens["access_token"], TEST_PASSWORD, "NewSecret456")

    assert response.status_code == 200
    assert response.json()["refresh_token"]
    assert (await _login(client, TEST_PASSWORD)).status_code == 401
    assert (await _login(client, "NewSecret456")).status_code == 200


async def test_change_password_signs_out_other_devices(client: AsyncClient) -> None:
    this_device = (await register(client))["tokens"]
    other_device = (await _login(client, TEST_PASSWORD)).json()["tokens"]

    new_tokens = (
        await _change(client, this_device["access_token"], TEST_PASSWORD, "NewSecret456")
    ).json()

    for old in (this_device, other_device):
        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old["refresh_token"]}
        )
        assert response.status_code == 401
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert response.status_code == 200


async def test_wrong_current_password(client: AsyncClient) -> None:
    tokens = (await register(client))["tokens"]

    response = await _change(client, tokens["access_token"], "Wrong1234", "NewSecret456")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wrong_password"


async def test_wrong_current_password_is_rate_limited(client: AsyncClient) -> None:
    tokens = (await register(client))["tokens"]
    for _ in range(5):
        await _change(client, tokens["access_token"], "Wrong1234", "NewSecret456")

    response = await _change(client, tokens["access_token"], TEST_PASSWORD, "NewSecret456")

    assert response.status_code == 429


async def test_new_password_must_differ(client: AsyncClient) -> None:
    tokens = (await register(client))["tokens"]

    response = await _change(client, tokens["access_token"], TEST_PASSWORD, TEST_PASSWORD)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "password_unchanged"


async def test_new_password_must_meet_policy(client: AsyncClient) -> None:
    tokens = (await register(client))["tokens"]

    response = await _change(client, tokens["access_token"], TEST_PASSWORD, "short1")

    assert response.status_code == 422


async def test_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": "NewSecret456"},
    )

    assert response.status_code == 401
