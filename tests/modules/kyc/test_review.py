import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import CliError, set_role
from app.common.enums import KycStatus, UserRole
from app.modules.kyc.models import KycAccessLog, KycEvent, KycProfile
from app.modules.kyc.provider.fake import FakeKycProvider
from app.modules.kyc.schemas import REJECTION_MESSAGES
from tests.helpers import bearer, register, start_registration
from tests.modules.kyc.test_digilocker import START, complete, kyc, move
from tests.modules.kyc.test_selfie import take_selfie

SUBMIT = "/api/v1/kyc/submit"
ADMIN = "/api/v1/admin/kyc"

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


async def make_admin(app: FastAPI, email: str) -> None:
    async with app.state.session_factory() as session:
        await set_role(session, email, UserRole.ADMIN)


@pytest.fixture
async def admin(client: AsyncClient, app: FastAPI) -> tuple[str, Headers]:
    body = await register(
        client, email="admin@example.com", phone="9000000009", full_name="Meera Admin"
    )
    await make_admin(app, "admin@example.com")
    return body["user"]["id"], bearer(body["tokens"]["access_token"])


@pytest.fixture
def admin_headers(admin: tuple[str, Headers]) -> Headers:
    return admin[1]


async def finish_steps(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    """DigiLocker with a matching PAN, then a passing selfie."""
    await complete(client, headers, provider)
    await take_selfie(client, headers, provider, db_session)


@pytest.fixture
async def submitted(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> dict[str, Any]:
    await finish_steps(client, headers, provider, db_session)
    response = await client.post(SUBMIT, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def error_code(response: Any) -> str:
    code: str = response.json()["error"]["code"]
    return code


async def reject(
    client: AsyncClient, admin_headers: Headers, user_id: str, **body: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{ADMIN}/{user_id}/reject", headers=admin_headers, json={"reason": "photo_mismatch"} | body
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


# ---- Submitting


async def test_submit_sends_kyc_for_review(
    submitted: dict[str, Any], db_session: AsyncSession, user_id: str
) -> None:
    assert (submitted["status"], submitted["can_start"], submitted["next_step"]) == (
        "pending_review",
        False,
        None,
    )
    event = await db_session.scalar(
        select(KycEvent).where(KycEvent.to_status == KycStatus.PENDING_REVIEW)
    )
    assert event is not None
    assert (str(event.user_id), event.reason, event.actor_id) == (user_id, "submitted", None)


async def test_submit_before_any_step(client: AsyncClient, headers: Headers) -> None:
    response = await client.post(SUBMIT, headers=headers)
    assert response.status_code == 409
    assert error_code(response) == "kyc_incomplete"


async def test_submit_without_selfie(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider)
    response = await client.post(SUBMIT, headers=headers)
    assert response.status_code == 409
    assert error_code(response) == "kyc_incomplete"


async def test_submit_without_verified_pan(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider, pan=None)
    response = await client.post(SUBMIT, headers=headers)
    assert response.status_code == 409


@pytest.mark.usefixtures("submitted")
async def test_submit_twice(client: AsyncClient, headers: Headers) -> None:
    response = await client.post(SUBMIT, headers=headers)
    assert response.status_code == 409
    assert error_code(response) == "kyc_already_submitted"


@pytest.mark.usefixtures("submitted")
async def test_details_are_frozen_under_review(client: AsyncClient, headers: Headers) -> None:
    assert (await client.post(START, headers=headers)).status_code == 409
    assert (await client.post("/api/v1/kyc/selfie", headers=headers)).status_code == 409
    response = await client.post("/api/v1/kyc/pan", headers=headers, json={"pan": "ABCPV1234F"})
    assert response.status_code == 409


# ---- Who can review


async def test_admin_routes_need_the_admin_role(client: AsyncClient, headers: Headers) -> None:
    some_id = uuid.uuid4()
    for method, path in (
        ("GET", ADMIN),
        ("GET", f"{ADMIN}/{some_id}"),
        ("POST", f"{ADMIN}/{some_id}/approve"),
        ("POST", f"{ADMIN}/{some_id}/reject"),
    ):
        response = await client.request(
            method, path, headers=headers, json={"reason": "other", "note": "x" * 5}
        )
        assert response.status_code == 403, path
        assert error_code(response) == "insufficient_role"
        assert (await client.request(method, path)).status_code == 401


async def test_removed_admin_loses_access_at_once(
    client: AsyncClient, app: FastAPI, admin_headers: Headers
) -> None:
    assert (await client.get(ADMIN, headers=admin_headers)).status_code == 200

    async with app.state.session_factory() as session:
        await set_role(session, "admin@example.com", UserRole.CUSTOMER)

    assert (await client.get(ADMIN, headers=admin_headers)).status_code == 403


async def test_admin_role_only_for_active_accounts(
    client: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    await start_registration(client, email="new@example.com", phone="9000000008")
    with pytest.raises(CliError, match="not active"):
        await set_role(db_session, "new@example.com", UserRole.ADMIN)
    with pytest.raises(CliError, match="No account"):
        await set_role(db_session, "nobody@example.com", UserRole.ADMIN)


async def test_admin_cannot_review_own_kyc(
    client: AsyncClient,
    app: FastAPI,
    admin: tuple[str, Headers],
    provider: FakeKycProvider,
    db_session: AsyncSession,
) -> None:
    admin_id, admin_headers = admin
    await finish_steps(client, admin_headers, provider, db_session)
    assert (await client.post(SUBMIT, headers=admin_headers)).status_code == 200

    detail = (await client.get(f"{ADMIN}/{admin_id}", headers=admin_headers)).json()
    assert detail["can_decide"] is False
    for action in ("approve", "reject"):
        response = await client.post(
            f"{ADMIN}/{admin_id}/{action}", headers=admin_headers, json={"reason": "name_mismatch"}
        )
        assert response.status_code == 403
        assert error_code(response) == "cannot_review_own_kyc"


# ---- Queue and details


@pytest.mark.usefixtures("submitted")
async def test_queue_lists_submissions_without_personal_details(
    client: AsyncClient, admin_headers: Headers, user_id: str
) -> None:
    page = (await client.get(ADMIN, headers=admin_headers)).json()

    assert (page["total"], page["limit"], page["offset"]) == (1, 50, 0)
    item = page["items"][0]
    assert item["user_id"] == user_id
    assert item["kyc_status"] == "pending_review"
    assert (item["aadhaar"], item["pan"]) == ("XXXX XXXX 1234", "ABCXX1234F")
    assert item["selfie_match_score"] == 92.5
    assert item["submitted_at"] is not None
    assert not {"address", "date_of_birth", "details"} & item.keys()


async def test_queue_oldest_submission_first(
    client: AsyncClient,
    app: FastAPI,
    admin_headers: Headers,
) -> None:
    first = await register(client, email="first@example.com", phone="9000000011")
    second = await register(client, email="second@example.com", phone="9000000012")
    for body in (first, second):
        await move(app, body["user"]["id"], KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW)

    page = (await client.get(ADMIN, headers=admin_headers)).json()
    assert [item["user_id"] for item in page["items"]] == [
        first["user"]["id"],
        second["user"]["id"],
    ]
    page = (await client.get(f"{ADMIN}?limit=1&offset=1", headers=admin_headers)).json()
    assert ([i["user_id"] for i in page["items"]], page["total"]) == ([second["user"]["id"]], 2)


@pytest.mark.usefixtures("submitted")
async def test_queue_by_status(client: AsyncClient, admin_headers: Headers) -> None:
    page = (await client.get(f"{ADMIN}?status=verified", headers=admin_headers)).json()
    assert (page["items"], page["total"]) == ([], 0)


@pytest.mark.parametrize("query", ["status=nope", "limit=0", "limit=101", "offset=-1"])
async def test_queue_bad_query(client: AsyncClient, admin_headers: Headers, query: str) -> None:
    assert (await client.get(f"{ADMIN}?{query}", headers=admin_headers)).status_code == 422


@pytest.mark.usefixtures("submitted")
async def test_detail_shows_everything_needed_and_is_logged(
    client: AsyncClient,
    admin: tuple[str, Headers],
    user_id: str,
    db_session: AsyncSession,
) -> None:
    admin_id, admin_headers = admin
    detail = (await client.get(f"{ADMIN}/{user_id}", headers=admin_headers)).json()

    assert detail["account"]["email"] == "asha@example.com"
    assert detail["kyc_status"] == "pending_review"
    assert detail["details"]["name"] == "Asha Verma"
    assert detail["details"]["address"].startswith("12 MG Road")
    assert detail["details"]["pan"] == "ABCXX1234F"
    assert detail["account_name_matches"] is True
    assert detail["pan_source"] == "digilocker"
    assert detail["selfie"] == {
        "is_live": True,
        "match_score": 92.5,
        "min_match_score": 80,
        "completed_at": detail["selfie"]["completed_at"],
    }
    assert detail["can_decide"] is True
    assert [(e["to_status"], e["reason"]) for e in detail["events"]] == [
        ("in_progress", "digilocker_completed"),
        ("pending_review", "submitted"),
    ]

    log = await db_session.scalar(select(KycAccessLog))
    assert log is not None
    assert (str(log.admin_id), str(log.user_id)) == (admin_id, user_id)


async def test_detail_of_unknown_user(client: AsyncClient, admin_headers: Headers) -> None:
    response = await client.get(f"{ADMIN}/{uuid.uuid4()}", headers=admin_headers)
    assert response.status_code == 404


# ---- Approving


@pytest.mark.usefixtures("submitted")
async def test_approve_unlocks_cards(
    client: AsyncClient,
    headers: Headers,
    admin: tuple[str, Headers],
    user_id: str,
    db_session: AsyncSession,
) -> None:
    admin_id, admin_headers = admin
    assert (await client.get("/api/v1/cards", headers=headers)).status_code == 403

    response = await client.post(f"{ADMIN}/{user_id}/approve", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json()["kyc_status"] == "verified"
    assert response.json()["can_decide"] is False
    assert (await kyc(client, headers))["status"] == "verified"
    assert (await client.get("/api/v1/cards", headers=headers)).status_code == 200
    event = await db_session.scalar(select(KycEvent).where(KycEvent.reason == "approved"))
    assert event is not None
    assert str(event.actor_id) == admin_id


@pytest.mark.usefixtures("submitted")
async def test_decide_twice(client: AsyncClient, admin_headers: Headers, user_id: str) -> None:
    assert (
        await client.post(f"{ADMIN}/{user_id}/approve", headers=admin_headers)
    ).status_code == 200

    for action in ("approve", "reject"):
        response = await client.post(
            f"{ADMIN}/{user_id}/{action}", headers=admin_headers, json={"reason": "name_mismatch"}
        )
        assert response.status_code == 409
        assert error_code(response) == "kyc_not_pending_review"


async def test_cannot_approve_what_was_not_submitted(
    client: AsyncClient, admin_headers: Headers, user_id: str
) -> None:
    response = await client.post(f"{ADMIN}/{user_id}/approve", headers=admin_headers)
    assert response.status_code == 409
    assert error_code(response) == "kyc_not_pending_review"


async def test_cannot_approve_without_pan_and_selfie(
    client: AsyncClient, app: FastAPI, admin_headers: Headers, user_id: str
) -> None:
    # Only reachable if data were changed outside the API; approval checks anyway.
    await move(app, user_id, KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW)

    response = await client.post(f"{ADMIN}/{user_id}/approve", headers=admin_headers)
    assert response.status_code == 409
    assert error_code(response) == "kyc_incomplete"


# ---- Rejecting


@pytest.mark.usefixtures("submitted")
async def test_reject_shows_the_reason_but_not_the_note(
    client: AsyncClient, headers: Headers, admin_headers: Headers, user_id: str
) -> None:
    detail = await reject(client, admin_headers, user_id, note="Selfie looks like a printout")

    assert detail["kyc_status"] == "rejected"
    assert detail["events"][-1]["note"] == "Selfie looks like a printout"
    body = await kyc(client, headers)
    assert (body["status"], body["can_start"], body["next_step"]) == (
        "rejected",
        True,
        "digilocker",
    )
    assert body["rejection_reason"] == "photo_mismatch"
    assert body["rejection_message"] == REJECTION_MESSAGES["photo_mismatch"]
    assert "printout" not in str(body)


@pytest.mark.usefixtures("submitted")
async def test_other_needs_a_note(
    client: AsyncClient, admin_headers: Headers, user_id: str
) -> None:
    for body in ({"reason": "other"}, {"reason": "other", "note": "   "}, {"reason": "nope"}):
        response = await client.post(f"{ADMIN}/{user_id}/reject", headers=admin_headers, json=body)
        assert response.status_code == 422, body


@pytest.mark.usefixtures("submitted")
async def test_rejection_frees_the_pan(
    client: AsyncClient,
    admin_headers: Headers,
    user_id: str,
    provider: FakeKycProvider,
    db_session: AsyncSession,
) -> None:
    await reject(client, admin_headers, user_id, reason="document_issue")

    profile = await db_session.scalar(select(KycProfile))
    assert profile is not None
    assert (profile.pan_hash, profile.pan_verified) == (None, False)
    # The PAN's real owner can now verify it on their own account.
    other = await register(client, email="ravi@example.com", phone="9000000001")
    other_headers = bearer(other["tokens"]["access_token"])
    await complete(client, other_headers, provider)
    assert (await kyc(client, other_headers))["details"]["pan_verified"] is True


@pytest.mark.usefixtures("submitted")
async def test_rejected_user_starts_again_from_digilocker(
    client: AsyncClient,
    headers: Headers,
    admin_headers: Headers,
    user_id: str,
    provider: FakeKycProvider,
    db_session: AsyncSession,
) -> None:
    await reject(client, admin_headers, user_id)
    assert (await client.post(SUBMIT, headers=headers)).status_code == 409

    await complete(client, headers, provider)
    body = await kyc(client, headers)
    assert (body["status"], body["next_step"], body["rejection_reason"]) == (
        "in_progress",
        "selfie",
        None,
    )
    await take_selfie(client, headers, provider, db_session)
    assert (await client.post(SUBMIT, headers=headers)).json()["status"] == "pending_review"
    events = await db_session.scalar(select(func.count()).select_from(KycEvent))
    assert events == 5  # in_progress, pending_review, rejected, in_progress, pending_review
