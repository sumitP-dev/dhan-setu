import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import UserStatus
from app.core.security import create_access_token
from app.modules.users.models import User
from tests.helpers import TEST_JWT_SECRET, bearer, register


async def test_get_me(client: AsyncClient) -> None:
    body = await register(client)

    response = await client.get("/api/v1/users/me", headers=bearer(body["tokens"]["access_token"]))

    assert response.status_code == 200
    assert response.json() == body["user"]


async def test_missing_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


async def test_wrong_auth_scheme(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users/me", headers={"Authorization": "Basic abc"})

    assert response.status_code == 401


async def test_invalid_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users/me", headers=bearer("garbage"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_expired_token_tells_client_to_refresh(client: AsyncClient) -> None:
    user_id = uuid.UUID((await register(client))["user"]["id"])
    expired = create_access_token(
        user_id,
        secret=SecretStr(TEST_JWT_SECRET),
        ttl=timedelta(minutes=15),
        now=datetime.now(UTC) - timedelta(hours=1),
    )

    response = await client.get("/api/v1/users/me", headers=bearer(expired))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_expired"


async def test_token_for_deleted_user(client: AsyncClient, db_session: AsyncSession) -> None:
    token = (await register(client))["tokens"]["access_token"]
    await db_session.execute(delete(User))
    await db_session.commit()

    response = await client.get("/api/v1/users/me", headers=bearer(token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_disabled_user(client: AsyncClient, db_session: AsyncSession) -> None:
    token = (await register(client))["tokens"]["access_token"]
    await db_session.execute(update(User).values(status=UserStatus.BLOCKED))
    await db_session.commit()

    response = await client.get("/api/v1/users/me", headers=bearer(token))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


async def test_update_name(client: AsyncClient) -> None:
    token = (await register(client))["tokens"]["access_token"]

    response = await client.patch(
        "/api/v1/users/me", json={"full_name": "  Asha V.  "}, headers=bearer(token)
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Asha V."
    me = await client.get("/api/v1/users/me", headers=bearer(token))
    assert me.json()["full_name"] == "Asha V."


async def test_empty_patch_changes_nothing(client: AsyncClient) -> None:
    body = await register(client)

    response = await client.patch(
        "/api/v1/users/me", json={}, headers=bearer(body["tokens"]["access_token"])
    )

    assert response.status_code == 200
    assert response.json() == body["user"]


async def test_email_and_phone_cannot_be_changed_here(client: AsyncClient) -> None:
    token = (await register(client))["tokens"]["access_token"]

    for field, value in (("email", "new@example.com"), ("phone", "9123456789")):
        response = await client.patch(
            "/api/v1/users/me", json={field: value}, headers=bearer(token)
        )
        assert response.status_code == 422


async def test_blank_name_is_rejected(client: AsyncClient) -> None:
    token = (await register(client))["tokens"]["access_token"]

    response = await client.patch(
        "/api/v1/users/me", json={"full_name": "   "}, headers=bearer(token)
    )

    assert response.status_code == 422
