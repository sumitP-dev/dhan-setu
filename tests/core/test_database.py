import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient
from sqlalchemy import String, func, select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.common.exceptions import ConflictError
from app.common.schemas import error_responses
from app.core.database import DbSession, TimestampMixin, UUIDPrimaryKeyMixin


class _TestBase(DeclarativeBase):
    """Separate metadata, so this table never leaks into app models or migrations."""


class Item(UUIDPrimaryKeyMixin, TimestampMixin, _TestBase):
    __tablename__ = "test_items"

    name: Mapped[str] = mapped_column(String(50), unique=True)


@pytest.fixture
async def items_table(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)


@pytest.fixture(autouse=True)
def _item_routes(app: FastAPI, items_table: None) -> None:
    @app.post(
        "/api/v1/_test/items",
        responses=error_responses(status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def create_item(db: DbSession, name: str, fail: bool = False) -> dict[str, str]:
        item = Item(name=name)
        db.add(item)
        await db.flush()
        if fail:
            raise ConflictError("Rejected after write")
        return {"id": str(item.id)}

    @app.post("/api/v1/_test/items/unflushed")
    async def create_unflushed(db: DbSession, name: str) -> dict[str, str]:
        # The INSERT only reaches the database at commit, after the handler returns.
        db.add(Item(name=name))
        return {"status": "accepted"}


async def _count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(Item)) or 0


async def test_successful_request_commits(client: AsyncClient, app: FastAPI) -> None:
    response = await client.post("/api/v1/_test/items", params={"name": "a"})

    assert response.status_code == 200
    assert await _count(app.state.session_factory) == 1


async def test_app_error_rolls_back(client: AsyncClient, app: FastAPI) -> None:
    response = await client.post("/api/v1/_test/items", params={"name": "a", "fail": True})

    assert response.status_code == 409
    assert await _count(app.state.session_factory) == 0


async def test_failed_commit_is_never_reported_as_success(
    client: AsyncClient, app: FastAPI
) -> None:
    await client.post("/api/v1/_test/items", params={"name": "dup"})

    response = await client.post("/api/v1/_test/items/unflushed", params={"name": "dup"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert await _count(app.state.session_factory) == 1


async def test_mixins_set_uuid_and_timestamps(db_session: AsyncSession) -> None:
    item = Item(name="x")
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)

    assert isinstance(item.id, uuid.UUID)
    assert item.created_at.tzinfo == UTC
    assert item.updated_at.tzinfo == UTC


async def test_naive_datetimes_are_refused(db_session: AsyncSession) -> None:
    item = Item(name="naive", created_at=datetime(2026, 1, 1))
    db_session.add(item)

    with pytest.raises(StatementError, match="Naive datetime"):
        await db_session.flush()
