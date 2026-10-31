from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import UserStatus
from app.core.database import utcnow
from app.modules.auth.models import OtpCode
from app.modules.users.models import User
from tests.helpers import (
    FakeRedis,
    FakeSmsSender,
    make_settings,
    register,
    start_registration,
    verify_otp,
)

PHONE = "+919876543210"


async def _send(client: AsyncClient, purpose: str, phone: str = "9876543210") -> Response:
    return await client.post("/api/v1/auth/otp/send", json={"phone": phone, "purpose": purpose})


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


# ---- verify_phone


async def test_wrong_code_is_rejected_and_account_stays_pending(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = (await start_registration(client))["otp"]["dev_otp"]

    response = await verify_otp(client, PHONE, _wrong(code))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_otp"
    user = await db_session.scalar(select(User))
    assert user is not None
    assert user.status == UserStatus.PENDING_VERIFICATION


async def test_code_works_only_once(client: AsyncClient) -> None:
    code = (await start_registration(client))["otp"]["dev_otp"]
    assert (await verify_otp(client, PHONE, code)).status_code == 200

    response = await verify_otp(client, PHONE, code)

    assert response.status_code == 400


async def test_expired_code_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    code = (await start_registration(client))["otp"]["dev_otp"]
    await db_session.execute(update(OtpCode).values(expires_at=utcnow() - timedelta(seconds=1)))
    await db_session.commit()

    response = await verify_otp(client, PHONE, code)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_otp"


async def test_five_wrong_attempts_kill_the_code(client: AsyncClient) -> None:
    code = (await start_registration(client))["otp"]["dev_otp"]
    for _ in range(5):
        assert (await verify_otp(client, PHONE, _wrong(code))).status_code == 400

    response = await verify_otp(client, PHONE, code)  # even the right code is refused now

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "otp_attempts_exceeded"


async def test_code_is_tied_to_its_purpose(client: AsyncClient) -> None:
    code = (await start_registration(client))["otp"]["dev_otp"]

    response = await verify_otp(client, PHONE, code, purpose="login")

    assert response.status_code == 400


async def test_resend_verify_code_for_pending_account(
    client: AsyncClient, fake_redis: FakeRedis, sms: FakeSmsSender
) -> None:
    first = (await start_registration(client))["otp"]["dev_otp"]
    fake_redis.expire_now("cooldown:")

    response = await _send(client, "verify_phone")

    assert response.status_code == 200
    second = response.json()["dev_otp"]
    assert sms.last.code == second
    # Only the newest code works.
    if first != second:
        assert (await verify_otp(client, PHONE, first)).status_code == 400
    assert (await verify_otp(client, PHONE, second)).status_code == 200


async def test_resend_cooldown(client: AsyncClient) -> None:
    await start_registration(client)

    response = await _send(client, "verify_phone")  # within 30s of registering

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "otp_cooldown"
    assert 0 < int(response.headers["retry-after"]) <= 30


async def test_cooldown_applies_to_unregistered_numbers_too(client: AsyncClient) -> None:
    """Otherwise the 429 (or its absence) would reveal whether a number is registered."""
    assert (await _send(client, "login", phone="9123456789")).status_code == 200

    assert (await _send(client, "login", phone="9123456789")).status_code == 429


async def test_no_verify_code_for_already_active_account(
    client: AsyncClient, sms: FakeSmsSender, fake_redis: FakeRedis
) -> None:
    await register(client)
    sent = len(sms.sent)
    fake_redis.expire_now("cooldown:")

    response = await _send(client, "verify_phone")

    assert response.status_code == 200
    assert len(sms.sent) == sent


# ---- login by OTP


async def test_login_with_otp(client: AsyncClient, fake_redis: FakeRedis) -> None:
    await register(client)
    fake_redis.expire_now("cooldown:")
    code = (await _send(client, "login")).json()["dev_otp"]

    response = await verify_otp(client, PHONE, code, purpose="login")

    assert response.status_code == 200
    assert response.json()["user"]["phone"] == PHONE
    assert response.json()["tokens"]["access_token"]


async def test_otp_login_does_not_reveal_unknown_numbers(
    client: AsyncClient, sms: FakeSmsSender, app: FastAPI
) -> None:
    await register(client)
    app.state.settings = make_settings(debug=False)  # as in production: no dev_otp
    known = await _send(client, "login", phone="9876543210")
    unknown = await _send(client, "login", phone="9123456789")

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [s.phone for s in sms.sent if s.purpose == "login"] == [PHONE]


async def test_otp_login_not_sent_to_pending_or_blocked_accounts(
    client: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession, fake_redis: FakeRedis
) -> None:
    await start_registration(client)
    assert (await _send(client, "login")).json().get("dev_otp") is None

    await db_session.execute(update(User).values(status=UserStatus.BLOCKED))
    await db_session.commit()
    fake_redis.expire_now("cooldown:")
    assert (await _send(client, "login")).json().get("dev_otp") is None

    assert all(s.purpose == "verify_phone" for s in sms.sent)


async def test_blocked_after_code_sent_cannot_sign_in(
    client: AsyncClient, db_session: AsyncSession, fake_redis: FakeRedis
) -> None:
    await register(client)
    fake_redis.expire_now("cooldown:")
    code = (await _send(client, "login")).json()["dev_otp"]
    await db_session.execute(update(User).values(status=UserStatus.BLOCKED))
    await db_session.commit()

    response = await verify_otp(client, PHONE, code, purpose="login")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


# ---- storage and input


async def test_codes_are_stored_hashed(client: AsyncClient, db_session: AsyncSession) -> None:
    code = (await start_registration(client))["otp"]["dev_otp"]

    otp = await db_session.scalar(select(OtpCode))

    assert otp is not None
    assert len(code) == 6
    assert code not in otp.code_hash
    assert code not in repr(otp)


async def test_dev_otp_hidden_without_debug(app: FastAPI, client: AsyncClient) -> None:
    app.state.settings = make_settings(debug=False)

    body = await start_registration(client)

    assert "dev_otp" not in body["otp"]


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", ""])
async def test_malformed_code_is_rejected(client: AsyncClient, code: str) -> None:
    response = await verify_otp(client, PHONE, code)

    assert response.status_code == 422


async def test_reset_password_purpose_is_not_open_yet(client: AsyncClient) -> None:
    response = await _send(client, "reset_password")

    assert response.status_code == 422
