import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import CardNetwork, CardType, KycStatus
from app.modules.admin.models import AdminAuditLog
from app.modules.admin.repository import is_expired
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.service import KycService
from app.modules.payments.gateway.base import GatewayCard
from app.modules.payments.gateway.fake import FakePaymentGateway
from tests.helpers import (
    add_saved_card,
    bearer,
    register,
    register_admin,
    register_kyc_verified,
)

USERS = "/api/v1/admin/users"
CARDS = "/api/v1/admin/cards"

Headers = dict[str, str]

RUPAY_DEBIT = GatewayCard(
    last4="7070",
    network=CardNetwork.RUPAY,
    card_type=CardType.DEBIT,
    issuer="SBI",
    expiry_month=6,
    expiry_year=2031,
)


@pytest.fixture
def gateway(app: FastAPI) -> FakePaymentGateway:
    gateway: FakePaymentGateway = app.state.payment_gateway
    return gateway


@pytest.fixture
async def admin(client: AsyncClient, app: FastAPI) -> tuple[str, Headers]:
    body, headers = await register_admin(client, app)
    return body["user"]["id"], headers


@pytest.fixture
def admin_headers(admin: tuple[str, Headers]) -> Headers:
    return admin[1]


@pytest.fixture
async def asha(client: AsyncClient, app: FastAPI) -> dict[str, Any]:
    """KYC-verified customer with two cards, one of them removed."""
    body = await register_kyc_verified(client, app)
    headers = bearer(body["tokens"]["access_token"])
    gateway: FakePaymentGateway = app.state.payment_gateway
    kept = await add_saved_card(client, headers, gateway)
    removed = await add_saved_card(client, headers, gateway, card=RUPAY_DEBIT)
    response = await client.delete(f"/api/v1/cards/{removed['id']}", headers=headers)
    assert response.status_code == 204, response.text
    return {"id": body["user"]["id"], "kept": kept, "removed": removed}


@pytest.fixture
async def ravi(client: AsyncClient) -> dict[str, Any]:
    body = await register(
        client, email="ravi@example.com", phone="9123456780", full_name="Ravi Kumar"
    )
    user: dict[str, Any] = body["user"]
    return user


async def get(client: AsyncClient, url: str, headers: Headers) -> dict[str, Any]:
    response = await client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def ids(page: dict[str, Any]) -> list[str]:
    return [item["id"] for item in page["items"]]


# ---- Access


async def test_needs_an_admin_portal_token(
    client: AsyncClient, app: FastAPI, ravi: dict[str, Any]
) -> None:
    admin_body, _ = await register_admin(client, app)
    app_token = bearer(admin_body["tokens"]["access_token"])  # the admin's mobile-app token
    for path in (USERS, f"{USERS}/{ravi['id']}", CARDS):
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=app_token)).status_code == 401


# ---- Users


@pytest.mark.usefixtures("asha")
async def test_users_newest_first_with_card_counts(
    client: AsyncClient, admin_headers: Headers, ravi: dict[str, Any], admin: tuple[str, Headers]
) -> None:
    page = await get(client, USERS, admin_headers)

    assert page["total"] == 3
    assert ids(page)[0] == ravi["id"]  # signed up last
    by_email = {item["email"]: item for item in page["items"]}
    asha = by_email["asha@example.com"]
    assert asha["phone"] == "+919876543210"
    assert (asha["kyc_status"], asha["status"], asha["role"]) == ("verified", "active", "customer")
    assert asha["active_cards"] == 1  # the removed card isn't counted
    assert by_email["ravi@example.com"]["active_cards"] == 0
    assert by_email["admin@example.com"]["role"] == "admin"


@pytest.mark.usefixtures("asha", "admin")
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("asha", ["asha@example.com"]),
        ("KUMAR", ["ravi@example.com"]),
        ("ravi@exa", ["ravi@example.com"]),
        ("98765", ["asha@example.com"]),
        ("+91 91234", ["ravi@example.com"]),
        ("%%", []),  # wildcards are taken literally
        ("__", []),
    ],
)
async def test_search_users(
    client: AsyncClient,
    admin_headers: Headers,
    ravi: dict[str, Any],
    query: str,
    expected: list[str],
) -> None:
    page = await client.get(USERS, headers=admin_headers, params={"q": query})
    assert sorted(item["email"] for item in page.json()["items"]) == expected


async def test_searches_are_recorded(
    client: AsyncClient, admin: tuple[str, Headers], db_session: AsyncSession
) -> None:
    admin_id, admin_headers = admin
    await client.get(USERS, headers=admin_headers, params={"q": "98765"})
    await client.get(CARDS, headers=admin_headers, params={"q": "1111"})
    await client.get(USERS, headers=admin_headers)  # plain listing isn't

    logs = (
        await db_session.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at))
    ).all()
    assert [(str(log.admin_id), log.action, log.detail) for log in logs] == [
        (admin_id, "search_users", "98765"),
        (admin_id, "search_cards", "1111"),
    ]


@pytest.mark.usefixtures("asha", "ravi")
async def test_filter_users(client: AsyncClient, admin_headers: Headers) -> None:
    verified = await get(client, f"{USERS}?kyc_status=verified", admin_headers)
    assert [item["email"] for item in verified["items"]] == ["asha@example.com"]

    page = await get(client, f"{USERS}?limit=1&offset=1", admin_headers)
    assert (len(page["items"]), page["total"], page["limit"], page["offset"]) == (1, 3, 1, 1)

    blocked = await get(client, f"{USERS}?status=blocked", admin_headers)
    assert blocked["total"] == 0


@pytest.mark.parametrize(
    "query", ["q=a", f"q={'x' * 101}", "kyc_status=nope", "status=nope", "limit=0", "limit=101"]
)
async def test_bad_user_queries(client: AsyncClient, admin_headers: Headers, query: str) -> None:
    assert (await client.get(f"{USERS}?{query}", headers=admin_headers)).status_code == 422


async def test_user_detail(
    client: AsyncClient,
    admin: tuple[str, Headers],
    asha: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    admin_id, admin_headers = admin
    response = await client.get(f"{USERS}/{asha['id']}", headers=admin_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["full_name"], body["kyc_status"], body["active_cards"]) == (
        "Asha Verma",
        "verified",
        1,
    )
    assert body["kyc"]["status"] == "verified"
    assert body["kyc"]["decided_at"] is not None
    cards = body["cards"]
    assert [(c["id"], c["status"]) for c in cards] == [
        (asha["kept"]["id"], "active"),
        (asha["removed"]["id"], "removed"),
    ]
    assert cards[0] | {"created_at": None} == {
        "id": asha["kept"]["id"],
        "last4": "1111",
        "network": "visa",
        "card_type": "credit",
        "issuer": "HDFC",
        "expiry_month": 12,
        "expiry_year": 2030,
        "expired": False,
        "is_default": True,
        "status": "active",
        "created_at": None,
        "removed_at": None,
    }
    assert cards[1]["removed_at"] is not None
    assert cards[1]["is_default"] is False
    assert "token" not in response.text  # gateway tokens never leave the backend
    assert "cust_" not in response.text

    log = await db_session.scalar(select(AdminAuditLog))
    assert log is not None
    assert (str(log.admin_id), log.action, str(log.target_user_id)) == (
        admin_id,
        "view_user",
        asha["id"],
    )


async def test_user_detail_shows_who_rejected_and_why(
    client: AsyncClient,
    app: FastAPI,
    admin: tuple[str, Headers],
    ravi: dict[str, Any],
) -> None:
    admin_id, admin_headers = admin
    async with app.state.session_factory() as session:
        service = KycService(KycRepository(session))
        user_id = uuid.UUID(ravi["id"])
        await service.transition(user_id, KycStatus.IN_PROGRESS, reason="digilocker_completed")
        await service.transition(user_id, KycStatus.PENDING_REVIEW, reason="submitted")
        await service.transition(
            user_id,
            KycStatus.REJECTED,
            reason="photo_mismatch",
            actor_id=uuid.UUID(admin_id),
            note="Blurry selfie",
        )
        await session.commit()

    kyc = (await get(client, f"{USERS}/{ravi['id']}", admin_headers))["kyc"]

    assert kyc["status"] == "rejected"
    assert kyc["submitted_at"] is not None
    assert (kyc["decided_by"], kyc["rejection_reason"], kyc["rejection_note"]) == (
        "Meera Admin",
        "photo_mismatch",
        "Blurry selfie",
    )


async def test_new_user_detail(
    client: AsyncClient, admin_headers: Headers, ravi: dict[str, Any]
) -> None:
    body = await get(client, f"{USERS}/{ravi['id']}", admin_headers)
    assert body["cards"] == []
    assert body["kyc"] == {
        "status": "not_submitted",
        "submitted_at": None,
        "decided_at": None,
        "decided_by": None,
        "rejection_reason": None,
        "rejection_note": None,
    }


async def test_unknown_user(client: AsyncClient, admin_headers: Headers) -> None:
    assert (await client.get(f"{USERS}/{uuid.uuid4()}", headers=admin_headers)).status_code == 404
    assert (await client.get(f"{USERS}/nope", headers=admin_headers)).status_code == 422


# ---- Cards


async def test_cards_with_owners(
    client: AsyncClient, admin_headers: Headers, asha: dict[str, Any]
) -> None:
    response = await client.get(CARDS, headers=admin_headers)
    page = response.json()

    assert page["total"] == 2
    assert ids(page) == [asha["removed"]["id"], asha["kept"]["id"]]  # newest first
    assert page["items"][1]["owner"] == {
        "id": asha["id"],
        "full_name": "Asha Verma",
        "phone": "+919876543210",
        "kyc_status": "verified",
    }
    assert "token" not in response.text


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("status=active", ["kept"]),
        ("status=removed", ["removed"]),
        ("network=rupay", ["removed"]),
        ("card_type=credit", ["kept"]),
        ("q=1111", ["kept"]),  # last 4 digits
        ("q=7070", ["removed"]),
        ("q=asha", ["removed", "kept"]),  # owner
        ("q=ravi", []),
    ],
)
async def test_filter_cards(
    client: AsyncClient,
    admin_headers: Headers,
    asha: dict[str, Any],
    query: str,
    expected: list[str],
) -> None:
    page = await get(client, f"{CARDS}?{query}", admin_headers)
    assert ids(page) == [asha[name]["id"] for name in expected]


@pytest.mark.parametrize("query", ["status=nope", "network=nope", "card_type=nope", "q=a"])
async def test_bad_card_queries(client: AsyncClient, admin_headers: Headers, query: str) -> None:
    assert (await client.get(f"{CARDS}?{query}", headers=admin_headers)).status_code == 422


def test_cards_expire_at_the_end_of_their_month() -> None:
    now = datetime(2026, 11, 12, tzinfo=UTC)
    assert not is_expired(11, 2026, now)
    assert is_expired(10, 2026, now)
    assert is_expired(12, 2025, now)
    assert not is_expired(1, 2027, now)
