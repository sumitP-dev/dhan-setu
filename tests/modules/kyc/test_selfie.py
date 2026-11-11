import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import KycStatus
from app.modules.kyc.models import KycSelfieSession
from app.modules.kyc.provider.fake import FakeKycProvider
from tests.helpers import bearer, make_settings, register
from tests.modules.kyc.test_digilocker import complete, kyc, move

SELFIE = "/api/v1/kyc/selfie"

Headers = dict[str, str]


@pytest.fixture
def provider(app: FastAPI) -> FakeKycProvider:
    provider: FakeKycProvider = app.state.kyc_provider
    return provider


@pytest.fixture
async def user(client: AsyncClient) -> tuple[str, Headers]:
    body = await register(client)
    return body["user"]["id"], bearer(body["tokens"]["access_token"])


@pytest.fixture
def user_id(user: tuple[str, Headers]) -> str:
    return user[0]


@pytest.fixture
def headers(user: tuple[str, Headers]) -> Headers:
    return user[1]


@pytest.fixture
async def ready(client: AsyncClient, headers: Headers, provider: FakeKycProvider) -> None:
    """DigiLocker done with a matching PAN: the next step is the selfie."""
    await complete(client, headers, provider)


async def start_selfie(client: AsyncClient, headers: Headers) -> dict[str, Any]:
    response = await client.post(SELFIE, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def poll_selfie(client: AsyncClient, headers: Headers, selfie_id: str) -> dict[str, Any]:
    response = await client.get(f"{SELFIE}/{selfie_id}", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def provider_selfie_id(db_session: AsyncSession, selfie_id: str) -> str:
    row = await db_session.scalar(
        select(KycSelfieSession).where(KycSelfieSession.id == uuid.UUID(selfie_id))
    )
    assert row is not None
    return row.provider_session_id


async def take_selfie(
    client: AsyncClient,
    headers: Headers,
    provider: FakeKycProvider,
    db_session: AsyncSession,
    **result: Any,
) -> dict[str, Any]:
    """Start a selfie, finish it in the fake SDK, poll. Returns the session."""
    selfie = await start_selfie(client, headers)
    provider.complete_selfie(await provider_selfie_id(db_session, selfie["id"]), **result)
    return await poll_selfie(client, headers, selfie["id"])


def error_code(response: Any) -> str:
    code: str = response.json()["error"]["code"]
    return code


# ---- Passing


@pytest.mark.usefixtures("ready")
async def test_live_matching_selfie_passes(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    assert (await kyc(client, headers))["next_step"] == "selfie"

    selfie = await start_selfie(client, headers)
    assert selfie["status"] == "pending"
    assert selfie["sdk_token"]
    provider_id = await provider_selfie_id(db_session, selfie["id"])
    assert provider.selfies[provider_id].session.sdk_token == selfie["sdk_token"]

    provider.complete_selfie(provider_id, is_live=True, match_score=92.5)
    result = await poll_selfie(client, headers, selfie["id"])

    assert result["status"] == "completed"
    assert result["failure_reason"] is None
    assert "sdk_token" not in result
    body = await kyc(client, headers)
    assert (body["status"], body["next_step"]) == ("in_progress", "submit")
    assert body["details"]["selfie_verified"] is True


@pytest.mark.usefixtures("ready")
async def test_score_exactly_at_the_minimum_passes(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    result = await take_selfie(client, headers, provider, db_session, match_score=80)
    assert result["status"] == "completed"


@pytest.mark.usefixtures("ready")
async def test_liveness_and_score_are_kept_for_review(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    selfie = await take_selfie(client, headers, provider, db_session, match_score=88.25)

    row = await db_session.scalar(
        select(KycSelfieSession).where(KycSelfieSession.id == uuid.UUID(selfie["id"]))
    )
    assert row is not None
    assert (row.is_live, row.match_score) == (True, 88.25)
    assert row.completed_at is not None


# ---- Starting


@pytest.mark.usefixtures("ready")
async def test_start_again_reuses_the_open_session_and_token(
    client: AsyncClient, headers: Headers
) -> None:
    first = await start_selfie(client, headers)
    second = await start_selfie(client, headers)
    assert (second["id"], second["sdk_token"]) == (first["id"], first["sdk_token"])


@pytest.mark.usefixtures("ready")
async def test_sdk_token_is_stored_encrypted(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    selfie = await start_selfie(client, headers)
    row = await db_session.scalar(select(KycSelfieSession))
    assert row is not None
    assert selfie["sdk_token"] not in row.sdk_token_encrypted
    assert row.sdk_token_encrypted.startswith("v1:")


async def test_selfie_needs_digilocker_first(client: AsyncClient, headers: Headers) -> None:
    response = await client.post(SELFIE, headers=headers)
    assert response.status_code == 409
    assert error_code(response) == "kyc_selfie_not_expected"


async def test_selfie_needs_a_verified_pan(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider, pan=None)
    assert (await kyc(client, headers))["next_step"] == "pan"

    response = await client.post(SELFIE, headers=headers)
    assert response.status_code == 409
    assert error_code(response) == "kyc_selfie_not_expected"


@pytest.mark.usefixtures("ready")
async def test_no_second_selfie_after_one_passed(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    await take_selfie(client, headers, provider, db_session)

    response = await client.post(SELFIE, headers=headers)
    assert response.status_code == 409
    assert error_code(response) == "selfie_already_verified"


@pytest.mark.usefixtures("ready")
async def test_no_selfie_while_under_review(
    client: AsyncClient, app: FastAPI, user_id: str, headers: Headers
) -> None:
    await move(app, user_id, KycStatus.PENDING_REVIEW)

    response = await client.post(SELFIE, headers=headers)
    assert response.status_code == 409


@pytest.mark.usefixtures("ready")
async def test_daily_selfie_limit(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    for _ in range(5):
        selfie = await start_selfie(client, headers)
        provider.fail_selfie(await provider_selfie_id(db_session, selfie["id"]))
        await poll_selfie(client, headers, selfie["id"])

    response = await client.post(SELFIE, headers=headers)
    assert response.status_code == 429
    assert error_code(response) == "selfie_attempt_limit"


async def test_selfie_routes_need_sign_in(client: AsyncClient) -> None:
    assert (await client.post(SELFIE)).status_code == 401
    assert (await client.get(f"{SELFIE}/{uuid.uuid4()}")).status_code == 401


# ---- Failing


@pytest.mark.usefixtures("ready")
@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ({"is_live": False, "match_score": 99}, "selfie_not_live"),
        ({"match_score": 79.9}, "face_mismatch"),
        ({"match_score": 0}, "face_mismatch"),
    ],
)
async def test_failed_checks(
    client: AsyncClient,
    headers: Headers,
    provider: FakeKycProvider,
    db_session: AsyncSession,
    result: dict[str, Any],
    reason: str,
) -> None:
    selfie = await take_selfie(client, headers, provider, db_session, **result)

    assert (selfie["status"], selfie["failure_reason"]) == ("failed", reason)
    assert selfie["failure_message"]
    body = await kyc(client, headers)
    assert body["next_step"] == "selfie"
    assert body["details"]["selfie_verified"] is False
    # A new attempt is allowed.
    assert (await start_selfie(client, headers))["id"] != selfie["id"]


@pytest.mark.usefixtures("ready")
@pytest.mark.parametrize(
    ("provider_reason", "reason"),
    [
        ("face_not_detected", "face_not_detected"),
        ("multiple_faces", "multiple_faces"),
        ("internal error <script>", "selfie_failed"),  # unknown reasons aren't passed on
    ],
)
async def test_provider_failures(
    client: AsyncClient,
    headers: Headers,
    provider: FakeKycProvider,
    db_session: AsyncSession,
    provider_reason: str,
    reason: str,
) -> None:
    selfie = await start_selfie(client, headers)
    provider.fail_selfie(await provider_selfie_id(db_session, selfie["id"]), provider_reason)

    result = await poll_selfie(client, headers, selfie["id"])
    assert (result["status"], result["failure_reason"]) == ("failed", reason)


@pytest.mark.usefixtures("ready")
async def test_expired_session(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    selfie = await start_selfie(client, headers)
    entry = provider.selfies[await provider_selfie_id(db_session, selfie["id"])]
    entry.session = replace(entry.session, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    result = await poll_selfie(client, headers, selfie["id"])
    assert (result["status"], result["failure_reason"]) == ("expired", "session_expired")


@pytest.mark.usefixtures("ready")
async def test_still_pending_until_the_sdk_finishes(client: AsyncClient, headers: Headers) -> None:
    selfie = await start_selfie(client, headers)
    assert (await poll_selfie(client, headers, selfie["id"]))["status"] == "pending"
    assert (await kyc(client, headers))["next_step"] == "selfie"


# ---- Aadhaar or status changing meanwhile


@pytest.mark.usefixtures("ready")
async def test_digilocker_again_clears_a_passed_selfie(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    await take_selfie(client, headers, provider, db_session)
    assert (await kyc(client, headers))["next_step"] == "submit"

    await complete(client, headers, provider)

    body = await kyc(client, headers)
    assert body["next_step"] == "selfie"
    assert body["details"]["selfie_verified"] is False


@pytest.mark.usefixtures("ready")
async def test_selfie_started_before_digilocker_ran_again_is_refused(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    old = await start_selfie(client, headers)
    await complete(client, headers, provider)  # new Aadhaar photo

    provider.complete_selfie(await provider_selfie_id(db_session, old["id"]))
    result = await poll_selfie(client, headers, old["id"])

    assert (result["status"], result["failure_reason"]) == ("failed", "aadhaar_changed")
    assert (await kyc(client, headers))["next_step"] == "selfie"
    # The new selfie is matched against the new photo.
    assert (await start_selfie(client, headers))["id"] != old["id"]


@pytest.mark.usefixtures("ready")
async def test_selfie_finishing_after_submission_changes_nothing(
    client: AsyncClient,
    app: FastAPI,
    user_id: str,
    headers: Headers,
    provider: FakeKycProvider,
    db_session: AsyncSession,
) -> None:
    selfie = await start_selfie(client, headers)
    await move(app, user_id, KycStatus.PENDING_REVIEW)

    provider.complete_selfie(await provider_selfie_id(db_session, selfie["id"]))
    result = await poll_selfie(client, headers, selfie["id"])

    assert (result["status"], result["failure_reason"]) == ("failed", "kyc_already_submitted")
    body = await kyc(client, headers)
    assert body["status"] == "pending_review"
    assert body["details"]["selfie_verified"] is False


# ---- Polling


@pytest.mark.usefixtures("ready")
async def test_finished_result_is_not_fetched_again(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    selfie = await take_selfie(client, headers, provider, db_session)
    provider.unavailable = True

    assert (await poll_selfie(client, headers, selfie["id"]))["status"] == "completed"


@pytest.mark.usefixtures("ready")
async def test_provider_down_keeps_the_session_open(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    selfie = await start_selfie(client, headers)
    provider.complete_selfie(await provider_selfie_id(db_session, selfie["id"]))
    provider.unavailable = True

    response = await client.get(f"{SELFIE}/{selfie['id']}", headers=headers)
    assert response.status_code == 503

    provider.unavailable = False
    assert (await poll_selfie(client, headers, selfie["id"]))["status"] == "completed"


@pytest.mark.usefixtures("ready")
async def test_cannot_read_another_users_selfie(client: AsyncClient, headers: Headers) -> None:
    selfie = await start_selfie(client, headers)
    other = await register(client, email="ravi@example.com", phone="9000000001")

    response = await client.get(
        f"{SELFIE}/{selfie['id']}", headers=bearer(other["tokens"]["access_token"])
    )
    assert response.status_code == 404


async def test_bad_selfie_id(client: AsyncClient, headers: Headers) -> None:
    assert (await client.get(f"{SELFIE}/not-a-uuid", headers=headers)).status_code == 422
    assert (await client.get(f"{SELFIE}/{uuid.uuid4()}", headers=headers)).status_code == 404


# ---- Settings


@pytest.mark.parametrize("score", [69, 101])
def test_min_match_score_must_be_70_to_100(score: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(kyc_selfie_min_match_score=score)
