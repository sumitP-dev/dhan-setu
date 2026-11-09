import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import (
    CardVerificationStatus,
    PaymentStatus,
    RefundStatus,
    WebhookEventStatus,
)
from app.modules.cards.models import Card, CardVerification
from app.modules.payments.gateway.base import (
    GatewayPaymentStatus,
    PaymentGatewayUnavailableError,
)
from app.modules.payments.gateway.fake import FakePaymentGateway
from app.modules.payments.models import Payment
from app.modules.webhooks.models import WebhookEvent
from app.modules.webhooks.router import MAX_WEBHOOK_BYTES
from tests.helpers import add_saved_card, bearer, new_idempotency_key, register

WEBHOOK = "/api/v1/webhooks/razorpay"


@pytest.fixture
def gateway(app: FastAPI) -> FakePaymentGateway:
    gateway: FakePaymentGateway = app.state.payment_gateway
    return gateway


@pytest.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    body = await register(client)
    return bearer(body["tokens"]["access_token"])


@pytest.fixture
async def card(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> dict[str, Any]:
    return await add_saved_card(client, auth, gateway)


def payment_event(event: str, order_id: str, **entity: Any) -> dict[str, Any]:
    return {
        "entity": "event",
        "event": event,
        "contains": ["payment"],
        "payload": {"payment": {"entity": {"id": "pay_x", "order_id": order_id} | entity}},
        "created_at": 1_700_000_000,
    }


def refund_event(event: str, refund_id: str, payment_id: str) -> dict[str, Any]:
    return {
        "entity": "event",
        "event": event,
        "payload": {"refund": {"entity": {"id": refund_id, "payment_id": payment_id}}},
    }


async def send(
    client: AsyncClient,
    gateway: FakePaymentGateway,
    payload: dict[str, Any] | bytes,
    *,
    event_id: str | None = None,
    signature: str | None = None,
) -> Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature or gateway.sign_webhook(body),
        "X-Razorpay-Event-Id": event_id or f"evt_{uuid.uuid4().hex[:14]}",
    }
    return await client.post(WEBHOOK, content=body, headers=headers)


async def new_payment(
    client: AsyncClient, auth: dict[str, str], card: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/payments/orders",
        headers=auth | {"Idempotency-Key": new_idempotency_key()},
        json={"amount_paise": 50_000, "card_id": card["id"]},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def card_token(db: AsyncSession, card_id: str) -> str:
    token = await db.scalar(select(Card.gateway_token_id).where(Card.id == uuid.UUID(card_id)))
    assert token is not None
    return token


async def payment_row(db: AsyncSession, order_id: str) -> Payment:
    db.expire_all()
    payment = await db.scalar(select(Payment).where(Payment.gateway_order_id == order_id))
    assert payment is not None
    return payment


async def webhook_rows(db: AsyncSession) -> list[WebhookEvent]:
    db.expire_all()
    return list(await db.scalars(select(WebhookEvent).order_by(WebhookEvent.created_at)))


# ---- Signature


@pytest.mark.parametrize("signature", [None, "", "0" * 64, "not-hex"])
async def test_unsigned_or_badly_signed_webhooks_are_rejected(
    client: AsyncClient,
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
    signature: str | None,
) -> None:
    body = json.dumps(payment_event("payment.captured", "order_AAAAAAAAAAAAAA")).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Razorpay-Signature"] = signature

    response = await client.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    assert await webhook_rows(db_session) == []


async def test_a_signature_for_another_body_is_rejected(
    client: AsyncClient, gateway: FakePaymentGateway
) -> None:
    signed = json.dumps(payment_event("payment.failed", "order_AAAAAAAAAAAAAA")).encode()
    tampered = json.dumps(payment_event("payment.captured", "order_AAAAAAAAAAAAAA")).encode()

    response = await send(client, gateway, tampered, signature=gateway.sign_webhook(signed))

    assert response.status_code == 400


async def test_oversized_bodies_are_refused(
    client: AsyncClient, gateway: FakePaymentGateway
) -> None:
    body = b"{" + b" " * MAX_WEBHOOK_BYTES + b"}"

    response = await send(client, gateway, body)

    assert response.status_code == 413


# ---- Payments


async def test_captured_webhook_completes_a_payment_the_app_never_verified(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    gateway.complete_checkout(order_id, token_id=await card_token(db_session, card["id"]))

    response = await send(client, gateway, payment_event("payment.captured", order_id))

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    assert (await payment_row(db_session, order_id)).status == PaymentStatus.SUCCESS
    [row] = await webhook_rows(db_session)
    assert (row.status, row.reference, row.event_type) == (
        WebhookEventStatus.PROCESSED,
        order_id,
        "payment.captured",
    )


async def test_the_payload_is_never_trusted_only_the_gateway(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    token = await card_token(db_session, card["id"])
    gateway.complete_checkout(order_id, token_id=token, status=GatewayPaymentStatus.FAILED)

    # Correctly signed, but claims success with a different amount.
    response = await send(
        client,
        gateway,
        payment_event("payment.captured", order_id, status="captured", amount=1),
    )

    assert response.json() == {"status": "processed"}
    payment = await payment_row(db_session, order_id)
    assert payment.status == PaymentStatus.FAILED  # what Razorpay says, not the payload
    assert payment.amount_paise == 50_000


async def test_failed_webhook_fails_the_payment(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    gateway.complete_checkout(
        order_id,
        token_id=await card_token(db_session, card["id"]),
        status=GatewayPaymentStatus.FAILED,
    )

    await send(client, gateway, payment_event("payment.failed", order_id))

    payment = await payment_row(db_session, order_id)
    assert (payment.status, payment.failure_code) == (PaymentStatus.FAILED, "payment_failed")


async def test_order_paid_event_is_handled_too(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    gateway.complete_checkout(order_id, token_id=await card_token(db_session, card["id"]))
    event = {"event": "order.paid", "payload": {"order": {"entity": {"id": order_id}}}}

    await send(client, gateway, event)

    assert (await payment_row(db_session, order_id)).status == PaymentStatus.SUCCESS


# ---- Duplicates and replays


async def test_the_same_event_is_handled_once(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    gateway.complete_checkout(order_id, token_id=await card_token(db_session, card["id"]))
    event = payment_event("payment.captured", order_id)

    first = await send(client, gateway, event, event_id="evt_SameEvent00001")
    again = await send(client, gateway, event, event_id="evt_SameEvent00001")
    # Same signed body under a new event id (the id isn't covered by the signature).
    replay = await send(client, gateway, event, event_id="evt_Replayed000001")

    assert first.json() == {"status": "processed"}
    assert again.json() == {"status": "duplicate"}
    assert replay.json() == {"status": "duplicate"}
    assert len(await webhook_rows(db_session)) == 1


async def test_gateway_outage_asks_razorpay_to_retry_and_the_retry_works(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    gateway.complete_checkout(order_id, token_id=await card_token(db_session, card["id"]))
    event = payment_event("payment.captured", order_id)

    gateway.unavailable = True
    failed = await send(client, gateway, event, event_id="evt_RetryMe0000001")
    assert failed.status_code == 503
    assert await webhook_rows(db_session) == []  # nothing kept, so the retry isn't a duplicate

    gateway.unavailable = False
    retried = await send(client, gateway, event, event_id="evt_RetryMe0000001")

    assert retried.json() == {"status": "processed"}
    assert (await payment_row(db_session, order_id)).status == PaymentStatus.SUCCESS


# ---- Card checks


async def test_webhook_finishes_a_card_check_the_app_never_completed(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway, db_session: AsyncSession
) -> None:
    started = await client.post(
        "/api/v1/cards/verifications", headers=auth | {"Idempotency-Key": new_idempotency_key()}
    )
    options = started.json()["checkout"]
    gateway.complete_checkout(
        options["order_id"], customer_id=options["customer_id"], save_card=True
    )

    response = await send(client, gateway, payment_event("payment.captured", options["order_id"]))

    assert response.json() == {"status": "processed"}
    db_session.expire_all()
    check = await db_session.scalar(
        select(CardVerification).where(CardVerification.gateway_order_id == options["order_id"])
    )
    assert check is not None
    assert (check.status, check.refund_status) == (
        CardVerificationStatus.VERIFIED,
        RefundStatus.PROCESSED,
    )
    assert len((await client.get("/api/v1/cards", headers=auth)).json()) == 1


async def test_webhook_resumes_a_card_check_stuck_after_the_charge(
    client: AsyncClient,
    auth: dict[str, str],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = (
        await client.post(
            "/api/v1/cards/verifications",
            headers=auth | {"Idempotency-Key": new_idempotency_key()},
        )
    ).json()
    options = started["checkout"]
    result = gateway.complete_checkout(
        options["order_id"], customer_id=options["customer_id"], save_card=True
    )
    real_fetch = gateway.fetch_card_token

    async def outage(*args: Any) -> Any:
        raise PaymentGatewayUnavailableError

    monkeypatch.setattr(gateway, "fetch_card_token", outage)
    charged = await client.post(
        f"/api/v1/cards/verifications/{started['id']}/complete",
        headers=auth,
        json={"payment_id": result.payment_id, "signature": result.signature},
    )
    assert charged.json()["status"] == "charged"

    monkeypatch.setattr(gateway, "fetch_card_token", real_fetch)
    await send(client, gateway, payment_event("payment.captured", options["order_id"]))

    status = (await client.get(f"/api/v1/cards/verifications/{started['id']}", headers=auth)).json()
    assert (status["status"], status["refund_status"]) == ("verified", "processed")
    assert len(gateway.refunds) == 1


# ---- Refunds


async def test_refund_webhook_adopts_a_refund_whose_request_timed_out(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    # Paid with a card that wasn't chosen, so it must be refunded...
    result = gateway.complete_checkout(order_id)
    real_refund = gateway.refund_payment
    refund_ids: list[str] = []

    async def times_out(payment_id: str, amount: int, *, receipt: str) -> Any:
        # ...and the refund is made at Razorpay, but the answer never reaches us.
        refund = await real_refund(payment_id, amount, receipt=receipt)
        refund_ids.append(refund.id)
        raise PaymentGatewayUnavailableError

    monkeypatch.setattr(gateway, "refund_payment", times_out)
    await client.post(
        f"/api/v1/payments/{order['payment']['id']}/verify",
        headers=auth,
        json={"payment_id": result.payment_id, "signature": result.signature},
    )
    payment = await payment_row(db_session, order_id)
    assert (payment.refund_status, payment.gateway_refund_id) == (RefundStatus.PENDING, None)

    await send(client, gateway, refund_event("refund.processed", refund_ids[0], result.payment_id))

    payment = await payment_row(db_session, order_id)
    assert (payment.refund_status, payment.gateway_refund_id) == (
        RefundStatus.PROCESSED,
        refund_ids[0],
    )


async def test_refund_made_in_the_dashboard_marks_the_payment_refunded(
    client: AsyncClient,
    auth: dict[str, str],
    card: dict[str, Any],
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
) -> None:
    order = await new_payment(client, auth, card)
    order_id = order["checkout"]["order_id"]
    result = gateway.complete_checkout(order_id, token_id=await card_token(db_session, card["id"]))
    await send(client, gateway, payment_event("payment.captured", order_id))
    refund = await gateway.refund_payment(result.payment_id, 50_000, receipt="dashboard")

    response = await send(
        client, gateway, refund_event("refund.processed", refund.id, result.payment_id)
    )

    assert response.json() == {"status": "processed"}
    assert (await payment_row(db_session, order_id)).status == PaymentStatus.REFUNDED


# ---- Things we don't handle


@pytest.mark.parametrize(
    "payload",
    [
        payment_event("payment.captured", "order_NotOurs000000001"),
        {"event": "subscription.charged", "payload": {}},
    ],
)
async def test_unknown_orders_and_event_types_are_ignored(
    client: AsyncClient, gateway: FakePaymentGateway, payload: dict[str, Any]
) -> None:
    response = await send(client, gateway, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"not json", "bad_payload"),
        ({"event": "payment.captured", "payload": {}}, "bad_payload"),
        (payment_event("payment.captured", "../../orders"), "invalid_gateway_id"),
        (refund_event("refund.processed", "rfnd_x", "pay_ABCDEFGH1234"), "invalid_gateway_id"),
    ],
)
async def test_signed_but_unusable_events_are_recorded_as_failed(
    client: AsyncClient,
    gateway: FakePaymentGateway,
    db_session: AsyncSession,
    payload: dict[str, Any] | bytes,
    error: str,
) -> None:
    response = await send(client, gateway, payload)

    # 200 so Razorpay stops retrying something that can never work.
    assert response.status_code == 200
    assert response.json() == {"status": "failed"}
    [row] = await webhook_rows(db_session)
    assert (row.status, row.error) == (WebhookEventStatus.FAILED, error)


async def test_payloads_are_not_stored(
    client: AsyncClient, gateway: FakePaymentGateway, db_session: AsyncSession
) -> None:
    event = payment_event("payment.captured", "order_NotOurs000000001", email="asha@example.com")

    await send(client, gateway, event)

    count = await db_session.scalar(select(func.count()).select_from(WebhookEvent))
    assert count == 1
    [row] = await webhook_rows(db_session)
    assert "asha" not in repr([getattr(row, c.key) for c in WebhookEvent.__table__.columns])
