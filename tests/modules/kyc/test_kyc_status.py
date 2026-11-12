import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import KycStatus
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.schemas import REJECTION_MESSAGES
from app.modules.kyc.service import KycService
from app.modules.kyc.state import (
    ALLOWED_TRANSITIONS,
    InvalidKycTransitionError,
    can_transition,
    ensure_transition,
)
from tests.helpers import bearer, mark_kyc_verified, new_idempotency_key, register

KYC = "/api/v1/kyc"
ME = "/api/v1/users/me"


@pytest.fixture
async def signed_in(client: AsyncClient) -> tuple[str, dict[str, str]]:
    body = await register(client)
    return body["user"]["id"], bearer(body["tokens"]["access_token"])


async def move(app: FastAPI, user_id: str, *targets: KycStatus, reason: str = "test") -> None:
    async with app.state.session_factory() as session:
        service = KycService(KycRepository(session))
        for target in targets:
            await service.transition(uuid.UUID(user_id), target, reason=reason)
        await session.commit()


# ---- Status rules


def test_every_status_has_rules() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(KycStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (KycStatus.NOT_SUBMITTED, KycStatus.IN_PROGRESS),
        (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW),
        (KycStatus.PENDING_REVIEW, KycStatus.VERIFIED),
        (KycStatus.PENDING_REVIEW, KycStatus.REJECTED),
        (KycStatus.REJECTED, KycStatus.IN_PROGRESS),
    ],
)
def test_allowed_changes(current: KycStatus, target: KycStatus) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Nobody gets verified without being reviewed.
        (KycStatus.NOT_SUBMITTED, KycStatus.VERIFIED),
        (KycStatus.IN_PROGRESS, KycStatus.VERIFIED),
        (KycStatus.REJECTED, KycStatus.VERIFIED),
        (KycStatus.NOT_SUBMITTED, KycStatus.PENDING_REVIEW),
        # Under review: the user can't restart or withdraw.
        (KycStatus.PENDING_REVIEW, KycStatus.IN_PROGRESS),
        (KycStatus.PENDING_REVIEW, KycStatus.NOT_SUBMITTED),
        # Verified is final.
        (KycStatus.VERIFIED, KycStatus.REJECTED),
        (KycStatus.VERIFIED, KycStatus.NOT_SUBMITTED),
        (KycStatus.VERIFIED, KycStatus.IN_PROGRESS),
    ],
)
def test_refused_changes(current: KycStatus, target: KycStatus) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidKycTransitionError):
        ensure_transition(current, target)


# ---- GET /kyc and /users/me


async def test_new_user_has_not_submitted_kyc(
    client: AsyncClient, signed_in: tuple[str, dict[str, str]]
) -> None:
    _, headers = signed_in

    kyc = await client.get(KYC, headers=headers)
    me = await client.get(ME, headers=headers)

    assert kyc.status_code == 200
    assert kyc.json() == {
        "status": "not_submitted",
        "can_start": True,
        "next_step": "digilocker",
        "details": None,
        "rejection_reason": None,
        "rejection_message": None,
    }
    assert me.json()["kyc_status"] == "not_submitted"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            (KycStatus.IN_PROGRESS,),
            {"status": "in_progress", "can_start": True, "next_step": "digilocker"},
        ),
        (
            (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW),
            {"status": "pending_review", "can_start": False, "next_step": None},
        ),
        (
            (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW, KycStatus.REJECTED),
            {
                "status": "rejected",
                "can_start": True,
                "next_step": "digilocker",
                # The test's reason isn't a known code, so it reads as "other".
                "rejection_reason": "other",
                "rejection_message": REJECTION_MESSAGES["other"],
            },
        ),
        (
            (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW, KycStatus.VERIFIED),
            {"status": "verified", "can_start": False, "next_step": None},
        ),
    ],
)
async def test_kyc_reports_each_status(
    client: AsyncClient,
    app: FastAPI,
    signed_in: tuple[str, dict[str, str]],
    path: tuple[KycStatus, ...],
    expected: dict[str, object],
) -> None:
    user_id, headers = signed_in
    await move(app, user_id, *path)

    response = await client.get(KYC, headers=headers)

    no_rejection = {"rejection_reason": None, "rejection_message": None}
    assert response.json() == no_rejection | expected | {"details": None}
    assert (await client.get(ME, headers=headers)).json()["kyc_status"] == expected["status"]


async def test_kyc_needs_sign_in(client: AsyncClient) -> None:
    response = await client.get(KYC)
    assert response.status_code == 401


# ---- Changing status


async def test_every_change_is_recorded(
    app: FastAPI, db_session: AsyncSession, signed_in: tuple[str, dict[str, str]]
) -> None:
    user_id, _ = signed_in
    await move(app, user_id, KycStatus.IN_PROGRESS, reason="digilocker_completed")
    await move(app, user_id, KycStatus.PENDING_REVIEW, reason="submitted")

    events = await KycRepository(db_session).events(uuid.UUID(user_id))

    assert [(e.from_status, e.to_status, e.reason) for e in events] == [
        (KycStatus.NOT_SUBMITTED, KycStatus.IN_PROGRESS, "digilocker_completed"),
        (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW, "submitted"),
    ]
    assert all(e.actor_id is None for e in events)


async def test_admin_actor_is_recorded(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await register(client)
    admin = await register(client, email="admin@example.com", phone="9000000009")
    user_id = uuid.UUID(user["user"]["id"])
    await move(app, user["user"]["id"], KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW)

    async with app.state.session_factory() as session:
        await KycService(KycRepository(session)).transition(
            user_id,
            KycStatus.VERIFIED,
            reason="approved",
            actor_id=uuid.UUID(admin["user"]["id"]),
        )
        await session.commit()

    last = (await KycRepository(db_session).events(user_id))[-1]
    assert last.actor_id == uuid.UUID(admin["user"]["id"])


async def test_refused_change_changes_nothing(
    app: FastAPI, client: AsyncClient, signed_in: tuple[str, dict[str, str]]
) -> None:
    user_id, headers = signed_in

    with pytest.raises(InvalidKycTransitionError):
        await move(app, user_id, KycStatus.VERIFIED)

    assert (await client.get(KYC, headers=headers)).json()["status"] == "not_submitted"


async def test_unknown_user_is_not_found(app: FastAPI) -> None:
    async with app.state.session_factory() as session:
        service = KycService(KycRepository(session))
        with pytest.raises(Exception, match="User not found"):
            await service.transition(uuid.uuid4(), KycStatus.IN_PROGRESS, reason="x")


async def test_status_changes_only_if_unchanged_since_read(
    app: FastAPI, client: AsyncClient, signed_in: tuple[str, dict[str, str]]
) -> None:
    """The second guard besides the row lock: an update based on a stale read does
    nothing. (Concurrent requests are checked against Postgres, which SQLite can't do.)"""
    user_id, headers = signed_in
    await move(app, user_id, KycStatus.IN_PROGRESS)

    async with app.state.session_factory() as session:
        repository = KycRepository(session)
        stale = await repository.set_status(
            uuid.UUID(user_id), current=KycStatus.NOT_SUBMITTED, target=KycStatus.IN_PROGRESS
        )
        fresh = await repository.set_status(
            uuid.UUID(user_id), current=KycStatus.IN_PROGRESS, target=KycStatus.PENDING_REVIEW
        )
        await session.commit()

    assert (stale, fresh) == (False, True)
    assert (await client.get(KYC, headers=headers)).json()["status"] == "pending_review"


# ---- Gate: cards and payments need verified KYC


GATED = [
    ("GET", "/api/v1/cards"),
    ("POST", "/api/v1/cards/verifications"),
    ("GET", f"/api/v1/cards/verifications/{uuid.uuid4()}"),
    ("PATCH", f"/api/v1/cards/{uuid.uuid4()}/default"),
    ("DELETE", f"/api/v1/cards/{uuid.uuid4()}"),
    ("POST", "/api/v1/payments/orders"),
    ("GET", f"/api/v1/payments/{uuid.uuid4()}"),
]


@pytest.mark.parametrize(
    "path",
    [
        (),
        (KycStatus.IN_PROGRESS,),
        (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW),
        (KycStatus.IN_PROGRESS, KycStatus.PENDING_REVIEW, KycStatus.REJECTED),
    ],
    ids=["not_submitted", "in_progress", "pending_review", "rejected"],
)
async def test_features_are_locked_until_verified(
    client: AsyncClient,
    app: FastAPI,
    signed_in: tuple[str, dict[str, str]],
    path: tuple[KycStatus, ...],
) -> None:
    user_id, headers = signed_in
    await move(app, user_id, *path)

    for method, url in GATED:
        response = await client.request(
            method, url, headers=headers | {"Idempotency-Key": new_idempotency_key()}
        )
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["error"]["code"] == "kyc_required"


async def test_verified_user_gets_through(
    client: AsyncClient, app: FastAPI, signed_in: tuple[str, dict[str, str]]
) -> None:
    user_id, headers = signed_in
    await mark_kyc_verified(app, user_id)

    response = await client.get("/api/v1/cards", headers=headers)

    assert response.status_code == 200
    assert response.json() == []


async def test_gate_needs_sign_in_first(client: AsyncClient) -> None:
    response = await client.get("/api/v1/cards")
    assert response.status_code == 401


def test_every_card_and_payment_route_is_gated() -> None:
    """A new card or payment route can't forget the KYC gate."""
    from fastapi.routing import APIRoute  # noqa: PLC0415

    from app.modules.cards.router import router as cards_router  # noqa: PLC0415
    from app.modules.kyc.dependencies import require_kyc_verified  # noqa: PLC0415
    from app.modules.payments.router import router as payments_router  # noqa: PLC0415

    def dependency_calls(route: APIRoute) -> set[object]:
        found: set[object] = set()
        stack = list(route.dependant.dependencies)
        while stack:
            dependant = stack.pop()
            found.add(dependant.call)
            stack.extend(dependant.dependencies)
        return found

    routes = [
        route
        for route in [*cards_router.routes, *payments_router.routes]
        if isinstance(route, APIRoute)
    ]
    assert len(routes) >= len(GATED)
    for route in routes:
        assert require_kyc_verified in dependency_calls(route), route.path
