from httpx import AsyncClient
from sqlalchemy import select

from app.modules.users.models import User
from tests.helpers import (
    TEST_PASSWORD,
    FakeSmsSender,
    bearer,
    login,
    register,
    start_registration,
    user_payload,
)


async def test_register_creates_pending_account_and_sends_otp(
    client: AsyncClient, sms: FakeSmsSender
) -> None:
    body = await start_registration(client, email="Asha@Example.com", phone="+91 98765 43210")

    assert "tokens" not in body  # no sign-in until the phone is verified
    user = body["user"]
    assert user["email"] == "asha@example.com"
    assert user["phone"] == "+919876543210"
    assert user["role"] == "customer"
    assert user["status"] == "pending_verification"
    assert user["kyc_status"] == "not_submitted"
    assert set(user) == {
        "id",
        "email",
        "phone",
        "full_name",
        "role",
        "status",
        "kyc_status",
        "created_at",
    }
    assert body["otp"]["expires_in"] == 5 * 60
    assert body["otp"]["resend_after"] == 30
    assert sms.last.phone == "+919876543210"
    assert sms.last.purpose == "verify_phone"
    assert body["otp"]["dev_otp"] == sms.last.code


async def test_verifying_otp_activates_account_and_signs_in(client: AsyncClient) -> None:
    body = await register(client)

    assert body["user"]["status"] == "active"
    assert body["tokens"]["token_type"] == "bearer"
    assert body["tokens"]["expires_in"] == 15 * 60
    me = await client.get("/api/v1/users/me", headers=bearer(body["tokens"]["access_token"]))
    assert me.status_code == 200
    assert me.json()["status"] == "active"


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


async def test_unverified_account_cannot_log_in(client: AsyncClient) -> None:
    await start_registration(client)

    response = await login(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "phone_not_verified"


async def test_unverified_account_with_wrong_password_reveals_nothing(
    client: AsyncClient,
) -> None:
    await start_registration(client)

    response = await login(client, password="Wrong1234")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_registration_rolls_back_cleanly_on_conflict(
    client: AsyncClient, sms: FakeSmsSender
) -> None:
    await start_registration(client)
    sent_before = len(sms.sent)

    response = await client.post("/api/v1/auth/register", json=user_payload(phone="9123456789"))

    assert response.status_code == 409
    assert len(sms.sent) == sent_before
