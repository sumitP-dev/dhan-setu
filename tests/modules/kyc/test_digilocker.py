import uuid
from dataclasses import replace
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import KycStatus
from app.modules.kyc.models import KycDigiLockerSession, KycProfile
from app.modules.kyc.provider.base import DigiLockerStatus, PanDetails
from app.modules.kyc.provider.fake import DEFAULT_AADHAAR, DEFAULT_PAN, FakeKycProvider
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.service import KycService
from tests.helpers import bearer, make_settings, register

KYC = "/api/v1/kyc"
START = f"{KYC}/digilocker"
CALLBACK = f"{KYC}/digilocker/callback"
PAN = f"{KYC}/pan"

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
def headers(user: tuple[str, Headers]) -> Headers:
    return user[1]


async def start(client: AsyncClient, headers: Headers) -> dict[str, Any]:
    response = await client.post(START, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def provider_session_id(provider: FakeKycProvider, consent_url: str) -> str:
    return consent_url.rsplit("/", 1)[-1]


async def poll(client: AsyncClient, headers: Headers, session_id: str) -> dict[str, Any]:
    response = await client.get(f"{START}/{session_id}", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def kyc(client: AsyncClient, headers: Headers) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(KYC, headers=headers)).json()
    return body


async def complete(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, **documents: Any
) -> dict[str, Any]:
    """Start DigiLocker, share documents in the fake, poll. Returns the session."""
    session = await start(client, headers)
    provider.complete_digilocker(provider_session_id(provider, session["consent_url"]), **documents)
    return await poll(client, headers, session["id"])


async def move(app: FastAPI, user_id: str, *targets: KycStatus) -> None:
    async with app.state.session_factory() as session:
        service = KycService(KycRepository(session))
        for target in targets:
            await service.transition(uuid.UUID(user_id), target, reason="test")
        await session.commit()


# ---- Start


async def test_start_returns_a_consent_link(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await start(client, headers)

    assert session["status"] == "pending"
    assert session["consent_url"].startswith("https://")
    assert session["failure_reason"] is None
    entry = provider.digilocker[provider_session_id(provider, session["consent_url"])]
    assert entry.redirect_url.startswith(
        "http://localhost:8000/api/v1/kyc/digilocker/callback?state="
    )
    assert entry.reference == f"kyc_{uuid.UUID(session['id']).hex}"


async def test_state_token_is_stored_only_as_a_hash(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    session = await start(client, headers)
    entry = provider.digilocker[provider_session_id(provider, session["consent_url"])]
    state = parse_qs(urlsplit(entry.redirect_url).query)["state"][0]

    row = await db_session.scalar(select(KycDigiLockerSession))
    assert row is not None
    assert state not in row.state_hash
    assert len(row.state_hash) == 64


async def test_start_again_reuses_the_open_session(client: AsyncClient, headers: Headers) -> None:
    first = await start(client, headers)
    second = await start(client, headers)
    assert second["id"] == first["id"]
    assert second["consent_url"] == first["consent_url"]


async def test_start_is_limited_per_day(
    app: FastAPI, client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    app.state.settings = make_settings(kyc_attempts_per_day=2)
    for _ in range(2):
        session = await start(client, headers)
        provider.end_digilocker(
            provider_session_id(provider, session["consent_url"]),
            DigiLockerStatus.DENIED,
            "consent_denied",
        )
        await poll(client, headers, session["id"])

    response = await client.post(START, headers=headers)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "kyc_attempt_limit"


async def test_cannot_start_while_under_review_or_verified(
    app: FastAPI, client: AsyncClient, user: tuple[str, Headers]
) -> None:
    user_id, headers = user
    await move(app, user_id, KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW)

    response = await client.post(START, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "kyc_not_startable"

    await move(app, user_id, KycStatus.VERIFIED)
    assert (await client.post(START, headers=headers)).status_code == 409


async def test_start_needs_sign_in(client: AsyncClient) -> None:
    assert (await client.post(START)).status_code == 401


async def test_start_when_kyc_is_off(client: AsyncClient, app: FastAPI, headers: Headers) -> None:
    from app.modules.kyc.provider.unavailable import UnavailableKycProvider  # noqa: PLC0415

    app.state.kyc_provider = UnavailableKycProvider()

    response = await client.post(START, headers=headers)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "kyc_not_available"
    assert (await kyc(client, headers))["status"] == "not_submitted"


async def test_provider_outage_on_start(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    provider.unavailable = True
    assert (await client.post(START, headers=headers)).status_code == 503
    provider.unavailable = False
    assert (await client.post(START, headers=headers)).status_code == 201


# ---- Completing DigiLocker


async def test_completed_digilocker_moves_kyc_to_in_progress(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await start(client, headers)
    assert (await poll(client, headers, session["id"]))["status"] == "pending"

    provider.complete_digilocker(provider_session_id(provider, session["consent_url"]))
    polled = await poll(client, headers, session["id"])

    assert polled["status"] == "completed"
    body = await kyc(client, headers)
    assert body["status"] == "in_progress"
    assert body["next_step"] == "selfie"
    assert body["details"] == {
        "name": "Asha Verma",
        "date_of_birth": "1994-05-17",
        "birth_year": 1994,
        "gender": "F",
        "address": "12 MG Road, Bengaluru, Karnataka 560001",
        "aadhaar": "XXXX XXXX 1234",
        "pan": "ABCXX1234F",
        "pan_verified": True,
        "pan_issue": None,
        "pan_issue_message": None,
        "selfie_verified": False,
        "fetched_at": body["details"]["fetched_at"],
    }


async def test_personal_details_are_encrypted_in_the_database(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    await complete(client, headers, provider)

    rows = (await db_session.execute(text("SELECT * FROM kyc_profiles"))).mappings().all()
    stored = " ".join(str(value) for value in rows[0].values())

    for secret in ("Asha", "ASHA", "1994-05-17", "MG Road", DEFAULT_PAN.number):
        assert secret not in stored
    assert "ABCXX1234F" in stored  # the masked PAN is fine to keep
    assert rows[0]["aadhaar_last4"] == "1234"


async def test_completion_is_recorded_once(
    app: FastAPI,
    client: AsyncClient,
    user: tuple[str, Headers],
    provider: FakeKycProvider,
    db_session: AsyncSession,
) -> None:
    user_id, headers = user
    session = await complete(client, headers, provider)
    await poll(client, headers, session["id"])
    await poll(client, headers, session["id"])

    events = await KycRepository(db_session).events(uuid.UUID(user_id))
    assert [(e.to_status, e.reason) for e in events] == [
        (KycStatus.IN_PROGRESS, "digilocker_completed")
    ]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (DigiLockerStatus.DENIED, "consent_denied"),
        (DigiLockerStatus.EXPIRED, "session_expired"),
        (DigiLockerStatus.FAILED, "digilocker_failed"),
    ],
)
async def test_unfinished_digilocker_changes_nothing(
    client: AsyncClient,
    headers: Headers,
    provider: FakeKycProvider,
    status: DigiLockerStatus,
    reason: str,
) -> None:
    session = await start(client, headers)
    provider.end_digilocker(provider_session_id(provider, session["consent_url"]), status, "x")

    polled = await poll(client, headers, session["id"])

    assert polled["status"] == status.value
    assert polled["failure_reason"] == reason
    assert polled["failure_message"]
    body = await kyc(client, headers)
    assert (body["status"], body["details"]) == ("not_submitted", None)


async def test_unsigned_aadhaar_is_refused(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await complete(
        client, headers, provider, aadhaar=replace(DEFAULT_AADHAAR, signature_verified=False)
    )

    assert session["status"] == "failed"
    assert session["failure_reason"] == "aadhaar_not_verified"
    assert (await kyc(client, headers))["status"] == "not_submitted"


async def test_another_users_session_is_not_found(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await start(client, headers)
    other = await register(client, email="ravi@example.com", phone="9000000001")

    response = await client.get(
        f"{START}/{session['id']}", headers=bearer(other["tokens"]["access_token"])
    )

    assert response.status_code == 404


async def test_provider_outage_while_polling(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await start(client, headers)
    provider.complete_digilocker(provider_session_id(provider, session["consent_url"]))
    provider.unavailable = True

    assert (await client.get(f"{START}/{session['id']}", headers=headers)).status_code == 503
    assert (await kyc(client, headers))["status"] == "not_submitted"

    provider.unavailable = False
    assert (await poll(client, headers, session["id"]))["status"] == "completed"


async def test_starting_again_in_progress_replaces_the_details(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider, pan=None)
    assert (await kyc(client, headers))["next_step"] == "pan"

    await complete(client, headers, provider)

    body = await kyc(client, headers)
    assert (body["status"], body["next_step"]) == ("in_progress", "selfie")


async def test_rejected_user_can_try_again(
    app: FastAPI, client: AsyncClient, user: tuple[str, Headers], provider: FakeKycProvider
) -> None:
    user_id, headers = user
    await complete(client, headers, provider)
    await move(app, user_id, KycStatus.PENDING_REVIEW, KycStatus.REJECTED)
    assert (await kyc(client, headers))["next_step"] == "digilocker"

    await complete(client, headers, provider)

    assert (await kyc(client, headers))["status"] == "in_progress"


async def test_details_under_review_are_never_replaced(
    app: FastAPI, client: AsyncClient, user: tuple[str, Headers], provider: FakeKycProvider
) -> None:
    user_id, headers = user
    await complete(client, headers, provider)
    second = await start(client, headers)
    await move(app, user_id, KycStatus.PENDING_REVIEW)

    other = replace(DEFAULT_AADHAAR, name="Someone Else", last4="9999")
    provider.complete_digilocker(
        provider_session_id(provider, second["consent_url"]), aadhaar=other
    )
    polled = await poll(client, headers, second["id"])

    assert polled["status"] == "failed"
    assert polled["failure_reason"] == "kyc_already_submitted"
    details = (await kyc(client, headers))["details"]
    assert (details["name"], details["aadhaar"]) == ("Asha Verma", "XXXX XXXX 1234")


# ---- Callback (the user's browser, no sign-in)


async def callback_for(provider: FakeKycProvider, session: dict[str, Any]) -> str:
    return provider.digilocker[provider_session_id(provider, session["consent_url"])].redirect_url


async def test_callback_finishes_the_session_and_opens_the_app(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await start(client, headers)
    provider.complete_digilocker(provider_session_id(provider, session["consent_url"]))
    url = urlsplit(await callback_for(provider, session))

    response = await client.get(f"{url.path}?{url.query}")

    assert response.status_code == 303
    assert response.headers["location"] == "dhansetu://kyc/digilocker?status=completed"
    assert (await kyc(client, headers))["status"] == "in_progress"

    again = await client.get(f"{url.path}?{url.query}")
    assert again.headers["location"] == "dhansetu://kyc/digilocker?status=completed"


async def test_callback_says_pending_when_the_user_hasnt_finished(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await start(client, headers)
    url = urlsplit(await callback_for(provider, session))

    response = await client.get(f"{url.path}?{url.query}")

    assert response.headers["location"] == "dhansetu://kyc/digilocker?status=pending"


async def test_callback_during_provider_outage_says_pending(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    session = await start(client, headers)
    provider.complete_digilocker(provider_session_id(provider, session["consent_url"]))
    provider.unavailable = True
    url = urlsplit(await callback_for(provider, session))

    response = await client.get(f"{url.path}?{url.query}")

    assert response.headers["location"] == "dhansetu://kyc/digilocker?status=pending"
    provider.unavailable = False
    assert (await poll(client, headers, session["id"]))["status"] == "completed"


@pytest.mark.parametrize("state", ["x" * 43, "short", "../../etc/passwd" + "a" * 27, ""])
async def test_callback_with_an_unknown_state_reveals_nothing(
    client: AsyncClient, state: str
) -> None:
    response = await client.get(CALLBACK, params={"state": state})

    assert response.status_code == 303
    assert response.headers["location"] == "dhansetu://kyc/digilocker?status=invalid"


async def test_callback_ignores_anything_but_the_state(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    """Extra query values (a forged status, someone else's session) change nothing."""
    session = await start(client, headers)
    url = urlsplit(await callback_for(provider, session))

    response = await client.get(
        f"{url.path}?{url.query}&status=completed&session_id={session['id']}"
    )

    assert response.headers["location"] == "dhansetu://kyc/digilocker?status=pending"
    assert (await kyc(client, headers))["status"] == "not_submitted"


# ---- PAN


async def test_pan_missing_from_digilocker_asks_for_it(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider, pan=None)

    body = await kyc(client, headers)
    assert body["next_step"] == "pan"
    assert body["details"]["pan"] is None
    assert body["details"]["pan_issue"] == "pan_not_in_digilocker"
    assert body["details"]["pan_issue_message"]


@pytest.mark.parametrize(
    ("pan", "issue"),
    [
        (
            PanDetails(number="ABCPV1234F", name="Someone Else", date_of_birth=None),
            "pan_name_mismatch",
        ),
        (
            PanDetails(number="ABCPV1234F", name="Asha Verma", date_of_birth=date(1990, 1, 1)),
            "pan_dob_mismatch",
        ),
        (
            PanDetails(number="ABCCV1234F", name="Asha Verma", date_of_birth=None),
            "pan_not_individual",
        ),
    ],
)
async def test_digilocker_pan_that_doesnt_match(
    client: AsyncClient,
    headers: Headers,
    provider: FakeKycProvider,
    pan: PanDetails,
    issue: str,
) -> None:
    await complete(client, headers, provider, pan=pan)

    body = await kyc(client, headers)
    assert body["next_step"] == "pan"
    assert body["details"]["pan_verified"] is False
    assert body["details"]["pan_issue"] == issue


async def test_pan_typed_in_is_checked_and_verified(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider, pan=None)

    response = await client.post(PAN, headers=headers, json={"pan": "abcpv1234f"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_step"] == "selfie"
    assert body["details"]["pan"] == "ABCXX1234F"
    assert body["details"]["pan_verified"] is True


@pytest.mark.parametrize(
    ("pan", "issue"),
    [
        ("ZZZPZ9999Z", "pan_invalid"),
        ("ABCCV1234F", "pan_not_individual"),
    ],
)
async def test_pan_typed_in_that_fails(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, pan: str, issue: str
) -> None:
    await complete(client, headers, provider, pan=None)

    body = (await client.post(PAN, headers=headers, json={"pan": pan})).json()

    assert body["details"]["pan_verified"] is False
    assert body["details"]["pan_issue"] == issue
    assert body["next_step"] == "pan"


async def test_pan_typed_in_with_another_name(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    provider.pan_records["XYZPA5678K"] = ("Someone Else", None)
    await complete(client, headers, provider, pan=None)

    body = (await client.post(PAN, headers=headers, json={"pan": "XYZPA5678K"})).json()

    assert body["details"]["pan_issue"] == "pan_name_mismatch"


@pytest.mark.parametrize("pan", ["12345", "ABCPV1234", "ABCPV-1234F"])
async def test_malformed_pan(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, pan: str
) -> None:
    await complete(client, headers, provider, pan=None)

    response = await client.post(PAN, headers=headers, json={"pan": pan})

    assert response.status_code == 422


async def test_pan_before_digilocker_is_refused(client: AsyncClient, headers: Headers) -> None:
    response = await client.post(PAN, headers=headers, json={"pan": "ABCPV1234F"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "kyc_pan_not_expected"


async def test_verified_pan_cannot_be_changed(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider)

    response = await client.post(PAN, headers=headers, json={"pan": "ABCPV1234F"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pan_already_verified"


async def test_pan_attempts_are_limited(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    await complete(client, headers, provider, pan=None)
    for _ in range(5):
        response = await client.post(PAN, headers=headers, json={"pan": "ZZZPZ9999Z"})
        assert response.status_code == 200

    response = await client.post(PAN, headers=headers, json={"pan": "ABCPV1234F"})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "pan_attempt_limit"


async def test_one_pan_cannot_verify_two_accounts(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider, db_session: AsyncSession
) -> None:
    await complete(client, headers, provider)
    other = await register(client, email="ravi@example.com", phone="9000000001")
    other_headers = bearer(other["tokens"]["access_token"])

    await complete(client, other_headers, provider)

    body = await kyc(client, other_headers)
    assert body["details"]["pan_verified"] is False
    assert body["details"]["pan_issue"] == "pan_already_used"
    assert body["next_step"] == "pan"
    owners = (
        await db_session.scalars(select(KycProfile).where(KycProfile.pan_hash.is_not(None)))
    ).all()
    assert len(owners) == 1


async def test_mismatched_pan_doesnt_block_the_real_owner(
    client: AsyncClient, headers: Headers, provider: FakeKycProvider
) -> None:
    """Someone who typed in another person's PAN (name mismatch) must not claim it."""
    other = await register(client, email="ravi@example.com", phone="9000000001")
    other_headers = bearer(other["tokens"]["access_token"])
    await complete(
        client,
        other_headers,
        provider,
        aadhaar=replace(DEFAULT_AADHAAR, name="Ravi Kumar"),
        pan=None,
    )
    await client.post(PAN, headers=other_headers, json={"pan": DEFAULT_PAN.number})

    await complete(client, headers, provider)

    assert (await kyc(client, headers))["details"]["pan_verified"] is True
