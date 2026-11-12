import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import set_role
from app.common.enums import OtpPurpose, UserRole, UserStatus
from app.core.database import utcnow
from app.core.security import ADMIN_AUDIENCE, create_access_token
from app.modules.admin_auth.models import AdminSession
from app.modules.users.models import User
from tests.helpers import (
    ADMIN_CLIENT,
    TEST_JWT_SECRET,
    TEST_PASSWORD,
    FakeSmsSender,
    admin_sign_in,
    bearer,
    make_admin,
    make_settings,
    register,
)

AUTH = "/api/v1/admin/auth"
EMAIL = "admin@example.com"
PHONE = "9000000009"
COOKIE = "ds_admin_refresh"


@pytest.fixture
async def admin(client: AsyncClient, app: FastAPI) -> dict[str, Any]:
    body = await register(client, email=EMAIL, phone=PHONE, full_name="Meera Admin")
    await make_admin(app, EMAIL)
    return body


async def login(client: AsyncClient, password: str = TEST_PASSWORD, email: str = EMAIL) -> Response:
    return await client.post(
        f"{AUTH}/login", headers=ADMIN_CLIENT, json={"email": email, "password": password}
    )


async def verify(client: AsyncClient, challenge: str, code: str) -> Response:
    return await client.post(
        f"{AUTH}/verify", headers=ADMIN_CLIENT, json={"challenge_token": challenge, "code": code}
    )


async def refresh(client: AsyncClient) -> Response:
    return await client.post(f"{AUTH}/refresh", headers=ADMIN_CLIENT)


async def me(client: AsyncClient, access_token: str) -> Response:
    return await client.get(f"{AUTH}/me", headers=ADMIN_CLIENT | bearer(access_token))


def error_code(response: Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


async def only_session(db_session: AsyncSession) -> AdminSession:
    row = await db_session.scalar(select(AdminSession).execution_options(populate_existing=True))
    assert row is not None
    return row


# ---- Step 1: password


@pytest.mark.usefixtures("admin")
async def test_password_step_texts_a_code(client: AsyncClient, sms: FakeSmsSender) -> None:
    response = await login(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["phone_hint"] == "+91 XXXXXX0009"
    assert body["challenge_token"]
    assert (sms.last.phone, sms.last.purpose) == ("+91" + PHONE, OtpPurpose.ADMIN_LOGIN)
    assert body["dev_otp"] == sms.last.code
    assert response.headers["cache-control"] == "no-store"


async def test_customers_get_the_same_error_as_a_wrong_password(
    client: AsyncClient, app: FastAPI, admin: dict[str, Any], sms: FakeSmsSender
) -> None:
    await register(client, email="asha@example.com", phone="9876543210")
    sent_before = len(sms.sent)

    wrong_password = await login(client, password="Wrong1234")
    customer = await login(client, email="asha@example.com")
    unknown = await login(client, email="nobody@example.com")

    for response in (wrong_password, customer, unknown):
        assert response.status_code == 401
        assert response.json() == wrong_password.json()
    assert error_code(wrong_password) == "invalid_credentials"
    assert len(sms.sent) == sent_before  # no code for any of them


@pytest.mark.usefixtures("admin")
async def test_blocked_admin_cannot_sign_in(client: AsyncClient, app: FastAPI) -> None:
    async with app.state.session_factory() as session:
        await session.execute(update(User).values(status=UserStatus.BLOCKED))
        await session.commit()

    assert error_code(await login(client)) == "invalid_credentials"


@pytest.mark.usefixtures("admin")
async def test_too_many_wrong_passwords(client: AsyncClient) -> None:
    for _ in range(5):
        assert (await login(client, password="Wrong1234")).status_code == 401
    response = await login(client)  # even the right password, during the lockout
    assert response.status_code == 429


@pytest.mark.usefixtures("admin")
async def test_one_code_per_cooldown(client: AsyncClient) -> None:
    assert (await login(client)).status_code == 200
    assert (await login(client)).status_code == 429


# ---- Cross-site protection


@pytest.mark.usefixtures("admin")
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Admin-Client": "something-else"}, ADMIN_CLIENT | {"Origin": "https://evil.example"}],
    ids=["no-header", "wrong-header", "other-origin"],
)
async def test_cookie_endpoints_refuse_other_sites(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    for path in ("/login", "/verify", "/refresh", "/logout", "/me"):
        response = await client.post(f"{AUTH}{path}", headers=headers, json={})
        if path == "/me":
            response = await client.get(f"{AUTH}{path}", headers=headers)
        assert response.status_code == 403, path
        assert error_code(response) == "origin_not_allowed"


@pytest.mark.usefixtures("admin")
async def test_portal_origin_is_allowed(client: AsyncClient) -> None:
    response = await client.post(
        f"{AUTH}/login",
        headers=ADMIN_CLIENT | {"Origin": "http://localhost:5173"},
        json={"email": EMAIL, "password": "Secret123"},
    )
    assert response.status_code == 200


async def test_cors_preflight_only_for_the_portal(client: AsyncClient) -> None:
    def preflight(origin: str) -> dict[str, str]:
        return {
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-admin-client, content-type",
        }

    allowed = await client.options(f"{AUTH}/refresh", headers=preflight("http://localhost:5173"))
    refused = await client.options(f"{AUTH}/refresh", headers=preflight("https://evil.example"))

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in refused.headers


# ---- Step 2: code


async def test_code_step_starts_a_session(
    client: AsyncClient, admin: dict[str, Any], db_session: AsyncSession
) -> None:
    started = (await login(client)).json()
    response = await client.post(
        f"{AUTH}/verify",
        headers=ADMIN_CLIENT | {"User-Agent": "Portal test"},
        json={"challenge_token": started["challenge_token"], "code": started["dev_otp"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admin"] == {
        "id": admin["user"]["id"],
        "full_name": "Meera Admin",
        "email": EMAIL,
    }
    assert body["expires_in"] == 600
    assert "refresh_token" not in body
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for attribute in ("HttpOnly", "Path=/", "SameSite=strict"):
        assert attribute.lower() in cookie.lower(), attribute
    # The cookie lasts as long as the session at most (8 hours).
    max_age = int(cookie.lower().split("max-age=")[1].split(";")[0])
    assert 28790 <= max_age <= 28800
    assert response.headers["cache-control"] == "no-store"

    row = await only_session(db_session)
    assert row.user_agent == "Portal test"
    assert row.ip_address is not None
    assert row.idle_expires_at - row.created_at <= timedelta(minutes=30, seconds=5)
    assert row.refresh_token_hash != client.cookies[COOKIE]  # only the hash is stored
    assert (await me(client, body["access_token"])).json()["email"] == EMAIL


@pytest.mark.usefixtures("admin")
async def test_wrong_code(client: AsyncClient) -> None:
    started = (await login(client)).json()
    wrong = "000000" if started["dev_otp"] != "000000" else "111111"

    response = await verify(client, started["challenge_token"], wrong)
    assert (response.status_code, error_code(response)) == (400, "invalid_otp")
    for _ in range(4):
        await verify(client, started["challenge_token"], wrong)
    # Attempts used up: even the right code no longer works.
    response = await verify(client, started["challenge_token"], started["dev_otp"])
    assert response.status_code == 429


@pytest.mark.usefixtures("admin")
async def test_code_works_once(client: AsyncClient) -> None:
    started = (await login(client)).json()
    assert (await verify(client, started["challenge_token"], started["dev_otp"])).status_code == 200
    response = await verify(client, started["challenge_token"], started["dev_otp"])
    assert response.status_code == 400


@pytest.mark.usefixtures("admin")
async def test_customer_login_code_does_not_work(client: AsyncClient) -> None:
    started = (await login(client)).json()
    customer = await client.post("/api/v1/auth/otp/send", json={"phone": PHONE, "purpose": "login"})
    customer_code = customer.json()["dev_otp"]
    assert customer_code

    response = await verify(client, started["challenge_token"], customer_code)
    assert response.status_code == 400


async def test_challenge_must_be_genuine(client: AsyncClient, admin: dict[str, Any]) -> None:
    started = (await login(client)).json()
    user_id = uuid.UUID(admin["user"]["id"])
    fakes = [
        "not-a-token",
        started["challenge_token"][:-2] + "xx",
        admin["tokens"]["access_token"],  # an app access token isn't a challenge
        create_access_token(
            user_id,
            secret=make_settings().jwt_secret_key,
            ttl=timedelta(minutes=5),
            audience=ADMIN_AUDIENCE,
        ),
    ]
    for fake in fakes:
        response = await verify(client, fake, started["dev_otp"])
        assert (response.status_code, error_code(response)) == (401, "invalid_challenge")


async def test_role_removed_between_steps(client: AsyncClient, app: FastAPI) -> None:
    await register(client, email=EMAIL, phone=PHONE)
    await make_admin(app, EMAIL)
    started = (await login(client)).json()
    async with app.state.session_factory() as session:
        await set_role(session, EMAIL, UserRole.CUSTOMER)

    response = await verify(client, started["challenge_token"], started["dev_otp"])
    assert response.status_code == 401


# ---- Using the access token


@pytest.mark.usefixtures("admin")
async def test_admin_token_does_not_work_in_the_customer_app(client: AsyncClient) -> None:
    signed_in = await admin_sign_in(client, EMAIL)
    response = await client.get("/api/v1/users/me", headers=bearer(signed_in["access_token"]))
    assert (response.status_code, error_code(response)) == (401, "invalid_token")


async def test_admin_token_needs_a_session(client: AsyncClient, admin: dict[str, Any]) -> None:
    from pydantic import SecretStr  # noqa: PLC0415

    no_session = create_access_token(
        uuid.UUID(admin["user"]["id"]),
        secret=SecretStr(TEST_JWT_SECRET),
        ttl=timedelta(minutes=5),
        audience=ADMIN_AUDIENCE,
    )
    unknown_session = create_access_token(
        uuid.UUID(admin["user"]["id"]),
        secret=SecretStr(TEST_JWT_SECRET),
        ttl=timedelta(minutes=5),
        audience=ADMIN_AUDIENCE,
        session_id=uuid.uuid4(),
    )
    for token in (no_session, unknown_session):
        response = await me(client, token)
        assert (response.status_code, error_code(response)) == (401, "session_expired")


@pytest.mark.usefixtures("admin")
async def test_idle_session_ends(client: AsyncClient, db_session: AsyncSession) -> None:
    signed_in = await admin_sign_in(client, EMAIL)
    await db_session.execute(
        update(AdminSession).values(idle_expires_at=utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()

    assert error_code(await me(client, signed_in["access_token"])) == "session_expired"
    assert (await refresh(client)).status_code == 401


@pytest.mark.usefixtures("admin")
async def test_blocked_admin_is_cut_off(client: AsyncClient, app: FastAPI) -> None:
    signed_in = await admin_sign_in(client, EMAIL)
    async with app.state.session_factory() as session:
        await session.execute(update(User).values(status=UserStatus.BLOCKED))
        await session.commit()

    assert (await me(client, signed_in["access_token"])).status_code == 401


# ---- Refresh


@pytest.mark.usefixtures("admin")
async def test_refresh_replaces_the_cookie(client: AsyncClient, db_session: AsyncSession) -> None:
    await admin_sign_in(client, EMAIL)
    first_cookie = client.cookies[COOKIE]

    response = await refresh(client)

    assert response.status_code == 200, response.text
    assert client.cookies[COOKIE] != first_cookie
    assert (await me(client, response.json()["access_token"])).status_code == 200
    row = await only_session(db_session)
    assert row.idle_expires_at <= row.expires_at


@pytest.mark.usefixtures("admin")
async def test_two_tabs_refreshing_at_once(client: AsyncClient) -> None:
    await admin_sign_in(client, EMAIL)
    old_cookie = client.cookies[COOKIE]
    assert (await refresh(client)).status_code == 200

    # The slower tab still sent the old cookie: told to retry, session kept.
    client.cookies.set(COOKIE, old_cookie)
    response = await refresh(client)
    assert (response.status_code, error_code(response)) == (409, "refresh_in_progress")


@pytest.mark.usefixtures("admin")
async def test_copied_cookie_ends_the_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await admin_sign_in(client, EMAIL)
    stolen = client.cookies[COOKIE]
    refreshed = await refresh(client)
    current = client.cookies[COOKIE]
    await db_session.execute(
        update(AdminSession).values(rotated_at=utcnow() - timedelta(minutes=1))
    )
    await db_session.commit()

    client.cookies.set(COOKIE, stolen)
    assert error_code(await refresh(client)) == "session_expired"

    row = await only_session(db_session)
    assert (row.revoked_at is not None, row.revoke_reason) == (True, "reused_token")
    # Neither copy works any more.
    client.cookies.set(COOKIE, current)
    assert (await refresh(client)).status_code == 401
    assert (await me(client, refreshed.json()["access_token"])).status_code == 401


async def test_refresh_without_a_cookie(client: AsyncClient) -> None:
    response = await refresh(client)
    assert (response.status_code, error_code(response)) == (401, "session_expired")


@pytest.mark.usefixtures("admin")
async def test_refresh_after_role_removed(
    client: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    await admin_sign_in(client, EMAIL)
    async with app.state.session_factory() as session:
        await set_role(session, EMAIL, UserRole.CUSTOMER)

    assert (await refresh(client)).status_code == 401
    assert (await only_session(db_session)).revoke_reason == "not_admin"


# ---- Logout


@pytest.mark.usefixtures("admin")
async def test_logout_ends_access_at_once(client: AsyncClient, db_session: AsyncSession) -> None:
    signed_in = await admin_sign_in(client, EMAIL)

    response = await client.post(f"{AUTH}/logout", headers=ADMIN_CLIENT)

    assert response.status_code == 204
    assert 'ds_admin_refresh=""' in response.headers["set-cookie"]
    assert (await me(client, signed_in["access_token"])).status_code == 401
    assert (await refresh(client)).status_code == 401
    assert (await only_session(db_session)).revoke_reason == "logout"
    # Safe to retry.
    assert (await client.post(f"{AUTH}/logout", headers=ADMIN_CLIENT)).status_code == 204


# ---- Settings


async def test_secure_cookie_outside_local_development(client: AsyncClient, app: FastAPI) -> None:
    app.state.settings = make_settings(admin_cookie_secure=True)
    await register(client, email=EMAIL, phone=PHONE)
    await make_admin(app, EMAIL)
    started = (await login(client)).json()

    response = await verify(client, started["challenge_token"], started["dev_otp"])

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-ds_admin_refresh=")
    assert "secure" in cookie.lower()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"admin_cookie_secure": False}, "ADMIN_COOKIE_SECURE must be true"),
        ({"admin_origins": ["http://admin.dhansetu.example"]}, "ADMIN_ORIGINS must all be https"),
    ],
)
def test_production_admin_settings(overrides: dict[str, Any], message: str) -> None:
    production = {
        "environment": "production",
        "payment_gateway": "razorpay",
        "razorpay_key_id": "rzp_live_abc123",
        "razorpay_key_secret": "live-key-secret",
        "razorpay_webhook_secret": "live-webhook-secret",
        "kyc_provider": "none",
        "public_base_url": "https://api.dhansetu.example",
    }
    with pytest.raises(ValidationError, match=message):
        make_settings(**production, **overrides)
