from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import UserRole
from app.modules.auth.dependencies import require_roles
from app.modules.users.models import User
from tests.helpers import bearer, register


@pytest.fixture
def admin_route(app: FastAPI) -> str:
    @app.get("/api/v1/_test/admin")
    async def admin_only(
        user: Annotated[User, Depends(require_roles(UserRole.ADMIN, UserRole.MERCHANT))],
    ) -> dict[str, str]:
        return {"role": user.role}

    return "/api/v1/_test/admin"


async def test_customer_is_forbidden(client: AsyncClient, admin_route: str) -> None:
    token = (await register(client))["tokens"]["access_token"]

    response = await client.get(admin_route, headers=bearer(token))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MERCHANT])
async def test_allowed_roles_pass(
    client: AsyncClient, db_session: AsyncSession, admin_route: str, role: UserRole
) -> None:
    token = (await register(client))["tokens"]["access_token"]
    await db_session.execute(update(User).values(role=role))
    await db_session.commit()

    response = await client.get(admin_route, headers=bearer(token))

    assert response.status_code == 200
    assert response.json() == {"role": role}


async def test_requires_authentication(client: AsyncClient, admin_route: str) -> None:
    response = await client.get(admin_route)

    assert response.status_code == 401
