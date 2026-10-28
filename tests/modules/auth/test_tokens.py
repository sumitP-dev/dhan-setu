from datetime import timedelta

from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.modules.auth.models import RefreshToken
from tests.helpers import register


async def _refresh(client: AsyncClient, refresh_token: str) -> Response:
    return await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})


async def test_refresh_returns_new_pair(client: AsyncClient) -> None:
    tokens = (await register(client))["tokens"]

    response = await _refresh(client, tokens["refresh_token"])

    assert response.status_code == 200
    new = response.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert new["access_token"]


async def test_refresh_token_works_only_once(client: AsyncClient) -> None:
    tokens = (await register(client))["tokens"]
    await _refresh(client, tokens["refresh_token"])

    response = await _refresh(client, tokens["refresh_token"])

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh_token"


async def test_reusing_old_token_revokes_the_whole_session(client: AsyncClient) -> None:
    """If a stolen token is replayed, both the thief's and the user's tokens stop working."""
    stolen = (await register(client))["tokens"]["refresh_token"]
    current = (await _refresh(client, stolen)).json()["refresh_token"]

    assert (await _refresh(client, stolen)).status_code == 401

    assert (await _refresh(client, current)).status_code == 401


async def test_reuse_does_not_affect_other_devices(client: AsyncClient) -> None:
    phone = (await register(client))["tokens"]["refresh_token"]
    login = await client.post(
        "/api/v1/auth/login", json={"email": "asha@example.com", "password": "Secret123"}
    )
    tablet = login.json()["tokens"]["refresh_token"]
    await _refresh(client, phone)
    await _refresh(client, phone)  # replay -> phone's session revoked

    assert (await _refresh(client, tablet)).status_code == 200


async def test_expired_refresh_token_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = (await register(client))["tokens"]
    await db_session.execute(
        update(RefreshToken).values(expires_at=utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()

    response = await _refresh(client, tokens["refresh_token"])

    assert response.status_code == 401


async def test_unknown_refresh_token_is_rejected(client: AsyncClient) -> None:
    response = await _refresh(client, "made-up-token")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh_token"


async def test_refresh_tokens_are_stored_hashed(client: AsyncClient, app: FastAPI) -> None:
    refresh_token = (await register(client))["tokens"]["refresh_token"]

    async with app.state.session_factory() as session:
        stored = await session.scalar(select(RefreshToken.token_hash))

    assert stored is not None
    assert stored != refresh_token


async def test_logout_revokes_refresh_token(client: AsyncClient) -> None:
    tokens = (await register(client))["tokens"]

    response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 204
    assert (await _refresh(client, tokens["refresh_token"])).status_code == 401


async def test_logout_is_idempotent(client: AsyncClient, db_session: AsyncSession) -> None:
    tokens = (await register(client))["tokens"]

    for _ in range(2):
        response = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        )
        assert response.status_code == 204

    unknown = await client.post("/api/v1/auth/logout", json={"refresh_token": "nope"})
    assert unknown.status_code == 204
    active = await db_session.scalar(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.revoked_at.is_(None))
    )
    assert active == 0
