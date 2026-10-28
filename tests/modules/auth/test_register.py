from httpx import AsyncClient
from sqlalchemy import select

from app.modules.users.models import User
from tests.helpers import TEST_PASSWORD, bearer, register, user_payload


async def test_register_returns_user_and_tokens(client: AsyncClient) -> None:
    body = await register(client, email="Asha@Example.com", phone="+91 98765 43210")

    assert body["user"]["email"] == "asha@example.com"
    assert body["user"]["phone"] == "+919876543210"
    assert body["user"]["full_name"] == "Asha Verma"
    assert set(body["user"]) == {"id", "email", "phone", "full_name", "created_at"}
    assert body["tokens"]["token_type"] == "bearer"
    assert body["tokens"]["expires_in"] == 15 * 60

    me = await client.get("/api/v1/users/me", headers=bearer(body["tokens"]["access_token"]))
    assert me.status_code == 200


async def test_password_is_stored_hashed(client: AsyncClient, db_session) -> None:  # type: ignore[no-untyped-def]
    await register(client)

    user = await db_session.scalar(select(User))

    assert user is not None
    assert user.password_hash != TEST_PASSWORD
    assert user.password_hash.startswith("$2")
    assert TEST_PASSWORD not in repr(user)


async def test_duplicate_email_is_rejected_case_insensitively(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        "/api/v1/auth/register", json=user_payload(email="ASHA@example.com", phone="9123456789")
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


async def test_duplicate_phone_is_rejected_in_any_format(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        "/api/v1/auth/register", json=user_payload(email="other@example.com", phone="+919876543210")
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "phone_taken"


async def test_weak_password_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=user_payload(password="password"))

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "validation_error",
        "message": "body.password: Must contain at least one letter and one number",
    }


async def test_invalid_phone_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=user_payload(phone="12345"))

    assert response.status_code == 422
    assert "Indian mobile number" in response.json()["error"]["message"]


async def test_unknown_fields_are_rejected(client: AsyncClient) -> None:
    payload = user_payload() | {"is_active": False}

    response = await client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 422
    assert "is_active" in response.json()["error"]["message"]
