import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import PaymentStatus
from app.core.database import utcnow
from app.modules.cards.models import Card
from app.modules.cards.repository import CardRepository
from app.modules.payments.gateway.base import (
    GatewayPayment,
    GatewayPaymentStatus,
    PaymentGatewayUnavailableError,
)
from app.modules.payments.gateway.fake import CheckoutResult, FakePaymentGateway
from app.modules.payments.models import Payment, PaymentEvent
from app.modules.payments.repository import PaymentRepository
from app.modules.payments.service import PaymentService
from tests.helpers import (
    add_saved_card,
    bearer,
    make_settings,
    new_idempotency_key,
    register_kyc_verified,
)

PAYMENTS = "/api/v1/payments"


@pytest.fixture
def gateway(app: FastAPI) -> FakePaymentGateway:
    gateway: FakePaymentGateway = app.state.payment_gateway
    return gateway


@pytest.fixture
async def auth(client: AsyncClient, app: FastAPI) -> dict[str, str]:
    body = await register_kyc_verified(client, app)
    return bearer(body["tokens"]["access_token"])


@pytest.fixture
async def other_auth(client: AsyncClient, app: FastAPI) -> dict[str, str]:
    body = await register_kyc_verified(client, app, email="ravi@example.com", phone="9000000001")
    return bearer(body["tokens"]["access_token"])


@pytest.fixture
async def card(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> dict[str, Any]:
    return await add_saved_card(client, auth, gateway)


async def token_of(db: AsyncSession, card_id: str) -> str:
    token = await db.scalar(select(Card.gateway_token_id).where(Card.id == uuid.UUID(card_id)))
    assert token is not None
    return token


async def create(
    client: AsyncClient,
    auth: dict[str, str],
    body: dict[str, Any],
    key: str | None = None,
) -> Response:
    return await client.post(
        f"{PAYMENTS}/orders",
        headers=auth | {"Idempotency-Key": key or new_idempotency_key()},
        json=body,
    )


async def created(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], amount: int = 50_000
) -> dict[str, Any]:
    response = await create(client, auth, {"amount_paise": amount, "card_id": card["id"]})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def verify(
    client: AsyncClient, auth: dict[str, str], payment_id: str, result: CheckoutResult
) -> Response:
    return await client.post(
        f"{PAYMENTS}/{payment_id}/verify",
        headers=auth,
        json={"payment_id": result.payment_id, "signature": result.signature},
    )


async def pay_with_card(
    db: AsyncSession, gateway: FakePaymentGateway, order: dict[str, Any], card_id: str, **kw: Any
) -> CheckoutResult:
    token = await token_of(db, card_id)
    return gateway.complete_checkout(order["checkout"]["order_id"], token_id=token, **kw)


async def events(db: AsyncSession, payment_id: str) -> list[tuple[str | None, str, str]]:
    rows = await db.scalars(
        select(PaymentEvent)
        .where(PaymentEvent.payment_id == uuid.UUID(payment_id))
        .order_by(PaymentEvent.created_at)
    )
    return [(r.from_status, r.to_status, r.reason) for r in rows]


async def allow_sync_now(db: AsyncSession, payment_id: str, **values: Any) -> None:
    """Pretend the last gateway check was long ago (and set other columns)."""
    await db.execute(
        update(Payment).where(Payment.id == uuid.UUID(payment_id)).values(synced_at=None, **values)
    )
    await db.commit()


# ---- Happy path


async def test_pay_with_saved_card(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    payment = order["payment"]

    assert payment["status"] == "created"
    assert payment["amount_paise"] == 50_000
    assert payment["card"] == {"id": card["id"], "last4": "1111", "network": "visa"}
    assert order["checkout"]["key_id"] == gateway.key_id
    assert gateway.orders[order["checkout"]["order_id"]].amount_paise == 50_000

    result = await pay_with_card(db_session, gateway, order, card["id"])
    response = await verify(client, auth, payment["id"], result)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["paid_at"] is not None
    assert body["failure_code"] is None
    assert await events(db_session, payment["id"]) == [
        (None, "created", "order_created"),
        ("created", "pending", "checkout_verified"),
        ("pending", "success", "checkout_verified"),
    ]
    assert (await client.get(f"{PAYMENTS}/{payment['id']}", headers=auth)).json() == body


async def test_authorized_payment_is_captured(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    result = await pay_with_card(
        db_session, gateway, order, card["id"], status=GatewayPaymentStatus.AUTHORIZED
    )

    body = (await verify(client, auth, order["payment"]["id"], result)).json()

    assert body["status"] == "success"
    assert gateway.payments[result.payment_id].status == GatewayPaymentStatus.CAPTURED


async def test_description_is_kept(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any]
) -> None:
    response = await create(
        client, auth, {"amount_paise": 100, "card_id": card["id"], "description": "Rent"}
    )

    assert response.json()["payment"]["description"] == "Rent"


# ---- Idempotency


async def test_same_key_and_body_returns_the_same_payment(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], gateway: FakePaymentGateway
) -> None:
    orders_before = len(gateway.orders)
    key = new_idempotency_key()
    body = {"amount_paise": 50_000, "card_id": card["id"]}

    first = (await create(client, auth, body, key)).json()
    second = (await create(client, auth, body, key)).json()

    assert first == second
    assert len(gateway.orders) == orders_before + 1


async def test_same_key_with_a_different_body_is_refused(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any]
) -> None:
    key = new_idempotency_key()
    await create(client, auth, {"amount_paise": 50_000, "card_id": card["id"]}, key)

    response = await create(client, auth, {"amount_paise": 99_999, "card_id": card["id"]}, key)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_reused"


async def test_idempotency_key_is_required(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any]
) -> None:
    response = await client.post(
        f"{PAYMENTS}/orders", headers=auth, json={"amount_paise": 100, "card_id": card["id"]}
    )

    assert response.status_code == 422


async def test_verifying_again_is_safe(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    result = await pay_with_card(db_session, gateway, order, card["id"])

    first = await verify(client, auth, order["payment"]["id"], result)
    again = await verify(client, auth, order["payment"]["id"], result)

    assert first.json() == again.json()
    assert len(await events(db_session, order["payment"]["id"])) == 3


async def test_a_completed_payment_cannot_take_another_gateway_payment(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    await verify(
        client,
        auth,
        order["payment"]["id"],
        await pay_with_card(db_session, gateway, order, card["id"]),
    )
    other = await created(client, auth, card)
    other_result = await pay_with_card(db_session, gateway, other, card["id"])

    response = await verify(client, auth, order["payment"]["id"], other_result)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_already_completed"


# ---- Input checks


@pytest.mark.parametrize("amount", [99, 0, -100, 100.0, "100", True, None, 10**20])
async def test_amount_must_be_whole_paise_and_at_least_one_rupee(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], amount: Any
) -> None:
    response = await create(client, auth, {"amount_paise": amount, "card_id": card["id"]})

    assert response.status_code == 422


async def test_amount_above_the_limit_is_refused(
    app: FastAPI, client: AsyncClient, auth: dict[str, str], card: dict[str, Any]
) -> None:
    app.state.settings = make_settings(max_payment_amount_paise=10_000)

    response = await create(client, auth, {"amount_paise": 10_001, "card_id": card["id"]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "amount_too_large"


@pytest.mark.parametrize(
    "extra",
    [
        {"description": "line\nbreak"},
        {"description": ""},
        {"description": "x" * 101},
        {"status": "success"},
        {"currency": "USD"},
    ],
)
async def test_body_is_strictly_validated(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], extra: dict[str, Any]
) -> None:
    response = await create(client, auth, {"amount_paise": 100, "card_id": card["id"]} | extra)

    assert response.status_code == 422


async def test_only_your_own_active_saved_card_can_pay(
    client: AsyncClient,
    auth: dict[str, str],
    other_auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
) -> None:
    theirs = await add_saved_card(client, other_auth, gateway)
    mine_removed = await add_saved_card(client, auth, gateway)
    await client.delete(f"/api/v1/cards/{mine_removed['id']}", headers=auth)

    for card_id in (theirs["id"], mine_removed["id"], str(uuid.uuid4())):
        response = await create(client, auth, {"amount_paise": 100, "card_id": card_id})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "card_not_found"


async def test_expired_card_cannot_pay(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], db_session: AsyncSession
) -> None:
    await db_session.execute(
        update(Card).where(Card.id == uuid.UUID(card["id"])).values(expiry_year=2020)
    )
    await db_session.commit()

    response = await create(client, auth, {"amount_paise": 100, "card_id": card["id"]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "card_expired"


# ---- Forged or mismatched payments


async def test_wrong_signature_is_rejected(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    result = await pay_with_card(db_session, gateway, order, card["id"])

    response = await verify(
        client, auth, order["payment"]["id"], replace(result, signature="0" * 64)
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    assert await events(db_session, order["payment"]["id"]) == [(None, "created", "order_created")]


async def test_a_payment_for_another_order_cannot_be_used(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    small = await created(client, auth, card, amount=100)
    large = await created(client, auth, card, amount=5_00_000)
    paid_small = await pay_with_card(db_session, gateway, small, card["id"])

    response = await verify(client, auth, large["payment"]["id"], paid_small)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"


async def test_amount_mismatch_at_the_gateway_is_rejected(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    result = await pay_with_card(db_session, gateway, order, card["id"])
    gateway.payments[result.payment_id] = replace(
        gateway.payments[result.payment_id], amount_paise=100
    )

    response = await verify(client, auth, order["payment"]["id"], result)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "payment_mismatch"


# ---- Only the chosen card


async def test_payment_with_an_unsaved_card_is_refunded(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], gateway: FakePaymentGateway
) -> None:
    order = await created(client, auth, card)
    # The user typed a new card into checkout instead of using the saved one.
    result = gateway.complete_checkout(order["checkout"]["order_id"])

    body = (await verify(client, auth, order["payment"]["id"], result)).json()

    assert body["status"] == "failed"
    assert body["failure_code"] == "card_not_allowed"
    assert body["failure_message"]
    assert body["refund_status"] == "processed"
    assert gateway.payments[result.payment_id].status == GatewayPaymentStatus.REFUNDED


async def test_authorized_payment_with_another_card_is_never_captured(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], gateway: FakePaymentGateway
) -> None:
    order = await created(client, auth, card)
    result = gateway.complete_checkout(
        order["checkout"]["order_id"], status=GatewayPaymentStatus.AUTHORIZED
    )

    body = (await verify(client, auth, order["payment"]["id"], result)).json()

    assert body["status"] == "failed"
    assert body["failure_code"] == "card_not_allowed"
    assert body["refund_status"] is None  # nothing was taken
    assert gateway.payments[result.payment_id].status == GatewayPaymentStatus.AUTHORIZED


async def test_paying_with_a_different_saved_card_is_not_accepted(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    second = await add_saved_card(client, auth, gateway)
    order = await created(client, auth, card)
    result = await pay_with_card(db_session, gateway, order, second["id"])

    body = (await verify(client, auth, order["payment"]["id"], result)).json()

    assert body["failure_code"] == "card_not_allowed"
    assert body["refund_status"] == "processed"


# ---- Pending, polling and retries


async def test_capture_outage_leaves_pending_and_polling_finishes_it(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await created(client, auth, card)
    payment_id = order["payment"]["id"]
    result = await pay_with_card(
        db_session, gateway, order, card["id"], status=GatewayPaymentStatus.AUTHORIZED
    )
    real_capture = gateway.capture_payment

    async def outage(*args: Any) -> GatewayPayment:
        raise PaymentGatewayUnavailableError

    monkeypatch.setattr(gateway, "capture_payment", outage)
    assert (await verify(client, auth, payment_id, result)).json()["status"] == "pending"

    monkeypatch.setattr(gateway, "capture_payment", real_capture)
    await allow_sync_now(db_session, payment_id)
    body = (await client.get(f"{PAYMENTS}/{payment_id}", headers=auth)).json()

    assert body["status"] == "success"


async def test_polling_finds_a_payment_the_app_never_verified(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    await pay_with_card(db_session, gateway, order, card["id"])  # app closed before verify

    body = (await client.get(f"{PAYMENTS}/{order['payment']['id']}", headers=auth)).json()

    assert body["status"] == "success"
    assert ("pending", "success", "gateway_sync") in await events(
        db_session, order["payment"]["id"]
    )


async def test_polling_checks_the_gateway_at_most_once_per_interval(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await created(client, auth, card)
    calls = 0
    real = gateway.fetch_order_payments

    async def counting(order_id: str) -> list[GatewayPayment]:
        nonlocal calls
        calls += 1
        return await real(order_id)

    monkeypatch.setattr(gateway, "fetch_order_payments", counting)
    for _ in range(5):
        await client.get(f"{PAYMENTS}/{order['payment']['id']}", headers=auth)

    assert calls == 1


async def test_a_retry_after_a_failed_attempt_still_succeeds(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    payment_id = order["payment"]["id"]
    await pay_with_card(db_session, gateway, order, card["id"], status=GatewayPaymentStatus.FAILED)
    failed = (await client.get(f"{PAYMENTS}/{payment_id}", headers=auth)).json()
    assert (failed["status"], failed["failure_code"]) == ("failed", "payment_failed")

    # The user tries again in checkout on the same order, and it goes through.
    await pay_with_card(db_session, gateway, order, card["id"])
    await allow_sync_now(db_session, payment_id)
    body = (await client.get(f"{PAYMENTS}/{payment_id}", headers=auth)).json()

    assert body["status"] == "success"
    assert body["failure_code"] is None
    assert [e[1] for e in await events(db_session, payment_id)] == [
        "created",
        "failed",
        "pending",
        "success",
    ]


async def test_unpaid_payment_expires(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], db_session: AsyncSession
) -> None:
    order = await created(client, auth, card)
    payment_id = order["payment"]["id"]
    await allow_sync_now(db_session, payment_id, created_at=utcnow() - timedelta(minutes=31))

    body = (await client.get(f"{PAYMENTS}/{payment_id}", headers=auth)).json()

    assert (body["status"], body["failure_code"]) == ("failed", "payment_expired")


async def test_polling_still_answers_when_the_gateway_is_down(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any], gateway: FakePaymentGateway
) -> None:
    order = await created(client, auth, card)
    gateway.unavailable = True

    response = await client.get(f"{PAYMENTS}/{order['payment']['id']}", headers=auth)

    assert response.status_code == 200
    assert response.json()["status"] == "created"


async def test_gateway_down_at_create_makes_no_payment(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    gateway.unavailable = True

    response = await create(client, auth, {"amount_paise": 100, "card_id": card["id"]})

    assert response.status_code == 503
    assert await db_session.scalar(select(Payment.id)) is None


# ---- Webhook path (used by B4)


async def test_settle_order_marks_a_refund_done_at_the_gateway(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    result = await pay_with_card(db_session, gateway, order, card["id"])
    await verify(client, auth, order["payment"]["id"], result)
    await gateway.refund_payment(result.payment_id, 50_000, receipt="dashboard")

    service = PaymentService(
        payments=PaymentRepository(db_session),
        cards=CardRepository(db_session),
        gateway=gateway,
        settings=app.state.settings,
    )
    payment = await service.settle_order(order["checkout"]["order_id"])

    assert payment is not None
    assert payment.status == PaymentStatus.REFUNDED
    assert await service.settle_order("order_NotOursAtAll01") is None


# ---- Access


async def test_users_cannot_see_or_verify_each_others_payments(
    client: AsyncClient,
    auth: dict[str, str],
    other_auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await created(client, auth, card)
    result = await pay_with_card(db_session, gateway, order, card["id"])
    url = f"{PAYMENTS}/{order['payment']['id']}"

    assert (await client.get(url, headers=other_auth)).status_code == 404
    assert (await verify(client, other_auth, order["payment"]["id"], result)).status_code == 404
    # The owner still gets their payment (polling finds it was paid).
    assert (await client.get(url, headers=auth)).json()["status"] == "success"


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", f"{PAYMENTS}/orders"), ("GET", f"{PAYMENTS}/{uuid.uuid4()}")],
)
async def test_payments_need_sign_in(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(
        method, path, headers={"Idempotency-Key": new_idempotency_key()}, json={}
    )

    assert response.status_code == 401
