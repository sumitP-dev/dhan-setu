import uuid
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.common.enums import CardType
from app.modules.payments.gateway.base import (
    GatewayPaymentStatus,
    PaymentGatewayUnavailableError,
)
from app.modules.payments.gateway.fake import DEFAULT_TEST_CARD, FakePaymentGateway
from tests.helpers import bearer, make_settings, register_kyc_verified

CARDS = "/api/v1/cards"
VERIFY = f"{CARDS}/verifications"


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


def new_key() -> str:
    return uuid.uuid4().hex


async def start(client: AsyncClient, auth: dict[str, str], key: str | None = None) -> Response:
    return await client.post(VERIFY, headers=auth | {"Idempotency-Key": key or new_key()})


async def started(client: AsyncClient, auth: dict[str, str]) -> dict[str, Any]:
    response = await start(client, auth)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def complete(
    client: AsyncClient, auth: dict[str, str], verification_id: str, payment_id: str, signature: str
) -> Response:
    return await client.post(
        f"{VERIFY}/{verification_id}/complete",
        headers=auth,
        json={"payment_id": payment_id, "signature": signature},
    )


async def add_card(
    client: AsyncClient,
    auth: dict[str, str],
    gateway: FakePaymentGateway,
    **checkout: Any,
) -> dict[str, Any]:
    """Start a check, pay ₹1 in (fake) checkout with save on, complete it."""
    verification = await started(client, auth)
    options = verification["checkout"]
    result = gateway.complete_checkout(
        options["order_id"],
        customer_id=checkout.pop("customer_id", options["customer_id"]),
        save_card=checkout.pop("save_card", True),
        **checkout,
    )
    response = await complete(client, auth, verification["id"], result.payment_id, result.signature)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---- Happy path


async def test_add_card_charges_one_rupee_saves_card_and_refunds(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    verification = await started(client, auth)

    assert verification["amount_paise"] == 100
    assert verification["status"] == "created"
    assert verification["checkout"]["key_id"] == gateway.key_id
    assert gateway.orders[verification["checkout"]["order_id"]].amount_paise == 100

    options = verification["checkout"]
    result = gateway.complete_checkout(
        options["order_id"], customer_id=options["customer_id"], save_card=True
    )
    response = await complete(client, auth, verification["id"], result.payment_id, result.signature)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "verified"
    assert body["refund_status"] == "processed"
    assert body["card"]["last4"] == "1111"
    assert body["card"]["network"] == "visa"
    assert body["card"]["is_default"] is True
    assert (body["card"]["expiry_month"], body["card"]["expiry_year"]) == (12, 2030)
    assert gateway.payments[result.payment_id].status == GatewayPaymentStatus.REFUNDED
    assert len(gateway.refunds) == 1

    cards = (await client.get(CARDS, headers=auth)).json()
    assert [c["id"] for c in cards] == [body["card"]["id"]]


async def test_card_responses_never_include_gateway_tokens(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    body = await add_card(client, auth, gateway)
    token_id = next(iter(gateway.tokens))

    listed = (await client.get(CARDS, headers=auth)).text
    assert token_id not in listed
    assert token_id not in str(body)
    assert "token" not in listed


async def test_authorized_payment_is_captured_then_refunded(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    body = await add_card(client, auth, gateway, status=GatewayPaymentStatus.AUTHORIZED)

    assert body["status"] == "verified"
    assert body["refund_status"] == "processed"


async def test_get_verification_shows_the_result(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    body = await add_card(client, auth, gateway)

    response = await client.get(f"{VERIFY}/{body['id']}", headers=auth)

    assert response.status_code == 200
    assert response.json() == body


# ---- Idempotency


async def test_same_idempotency_key_returns_the_same_check(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    key = new_key()

    first = (await start(client, auth, key)).json()
    second = (await start(client, auth, key)).json()

    assert first["id"] == second["id"]
    assert first["checkout"]["order_id"] == second["checkout"]["order_id"]
    assert len(gateway.orders) == 1


@pytest.mark.parametrize("key", [None, "short", "has spaces in it!!", "x" * 65])
async def test_idempotency_key_is_required_and_checked(
    client: AsyncClient, auth: dict[str, str], key: str | None
) -> None:
    headers = auth if key is None else auth | {"Idempotency-Key": key}

    response = await client.post(VERIFY, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_completing_again_is_safe_and_refunds_once(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    verification = await started(client, auth)
    options = verification["checkout"]
    result = gateway.complete_checkout(
        options["order_id"], customer_id=options["customer_id"], save_card=True
    )

    first = await complete(client, auth, verification["id"], result.payment_id, result.signature)
    again = await complete(client, auth, verification["id"], result.payment_id, result.signature)

    assert first.json() == again.json()
    assert len(gateway.refunds) == 1
    assert len((await client.get(CARDS, headers=auth)).json()) == 1


async def test_a_finished_check_cannot_take_a_different_payment(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    body = await add_card(client, auth, gateway)
    other = await started(client, auth)
    result = gateway.complete_checkout(other["checkout"]["order_id"])

    # A valid payment + signature, but for a different order.
    response = await complete(client, auth, body["id"], result.payment_id, result.signature)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "verification_finished"


# ---- Forged or mismatched payments


async def test_wrong_signature_is_rejected_and_nothing_is_saved(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    verification = await started(client, auth)
    result = gateway.complete_checkout(verification["checkout"]["order_id"])

    response = await complete(client, auth, verification["id"], result.payment_id, "0" * 64)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    status = (await client.get(f"{VERIFY}/{verification['id']}", headers=auth)).json()["status"]
    assert status == "created"
    assert gateway.refunds == {}


async def test_payment_for_another_order_cannot_verify_a_card(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    target = await started(client, auth)
    other = await started(client, auth)
    paid_elsewhere = gateway.complete_checkout(other["checkout"]["order_id"])

    response = await complete(
        client, auth, target["id"], paid_elsewhere.payment_id, paid_elsewhere.signature
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"


async def test_payment_with_a_different_amount_is_rejected(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    verification = await started(client, auth)
    result = gateway.complete_checkout(verification["checkout"]["order_id"])
    payment = gateway.payments[result.payment_id]
    gateway.payments[result.payment_id] = replace(payment, amount_paise=1)

    response = await complete(client, auth, verification["id"], result.payment_id, result.signature)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "payment_mismatch"
    assert (await client.get(CARDS, headers=auth)).json() == []


async def test_card_saved_for_another_customer_is_not_accepted(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    stranger = await gateway.create_customer(name="X", email="x@example.com", phone="+919999999999")

    body = await add_card(client, auth, gateway, customer_id=stranger)

    assert body["status"] == "failed"
    assert body["failure_code"] == "card_not_saved"
    assert body["refund_status"] == "processed"
    assert (await client.get(CARDS, headers=auth)).json() == []


@pytest.mark.parametrize(
    "body",
    [
        {"payment_id": "../../orders", "signature": "0" * 64},
        {"payment_id": "pay_ABCDEFGH12345", "signature": "not-hex"},
        {"payment_id": "pay_ABCDEFGH12345", "signature": "0" * 64, "card_last4": "4242"},
    ],
)
async def test_complete_body_is_strictly_validated(
    client: AsyncClient, auth: dict[str, str], body: dict[str, str]
) -> None:
    verification = await started(client, auth)

    response = await client.post(f"{VERIFY}/{verification['id']}/complete", headers=auth, json=body)

    assert response.status_code == 422


# ---- Card rejected or payment failed


async def test_failed_payment_saves_nothing_and_needs_no_refund(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    body = await add_card(client, auth, gateway, status=GatewayPaymentStatus.FAILED)

    assert body["status"] == "failed"
    assert body["failure_code"] == "payment_failed"
    assert body["failure_message"]
    assert body["refund_status"] is None
    assert gateway.refunds == {}


async def test_card_not_saved_at_checkout_fails_and_refunds(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    body = await add_card(client, auth, gateway, save_card=False)

    assert body["failure_code"] == "card_not_saved"
    assert body["refund_status"] == "processed"


@pytest.mark.parametrize(
    ("card", "code"),
    [
        (replace(DEFAULT_TEST_CARD, card_type=CardType.PREPAID), "card_type_not_supported"),
        (replace(DEFAULT_TEST_CARD, expiry_month=1, expiry_year=2020), "card_expired"),
    ],
)
async def test_unsupported_cards_are_refused_refunded_and_forgotten(
    client: AsyncClient,
    auth: dict[str, str],
    gateway: FakePaymentGateway,
    card: Any,
    code: str,
) -> None:
    body = await add_card(client, auth, gateway, card=card)

    assert body["status"] == "failed"
    assert body["failure_code"] == code
    assert body["refund_status"] == "processed"
    assert gateway.tokens == {}  # the token saved at checkout was deleted


# ---- Gateway trouble after the ₹1 was taken


async def test_gateway_outage_while_saving_keeps_the_charge_and_resumes(
    client: AsyncClient,
    auth: dict[str, str],
    gateway: FakePaymentGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification = await started(client, auth)
    options = verification["checkout"]
    result = gateway.complete_checkout(
        options["order_id"], customer_id=options["customer_id"], save_card=True
    )
    real_fetch = gateway.fetch_card_token

    async def outage(customer_id: str, token_id: str) -> Any:
        raise PaymentGatewayUnavailableError

    monkeypatch.setattr(gateway, "fetch_card_token", outage)
    first = await complete(client, auth, verification["id"], result.payment_id, result.signature)

    assert first.status_code == 200
    assert first.json()["status"] == "charged"
    assert gateway.refunds == {}

    monkeypatch.setattr(gateway, "fetch_card_token", real_fetch)
    second = await complete(client, auth, verification["id"], result.payment_id, result.signature)

    assert second.json()["status"] == "verified"
    assert second.json()["refund_status"] == "processed"
    assert len(gateway.refunds) == 1


async def test_refund_failure_keeps_the_card_and_leaves_refund_pending(
    client: AsyncClient,
    auth: dict[str, str],
    gateway: FakePaymentGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def outage(*args: Any, **kwargs: Any) -> Any:
        raise PaymentGatewayUnavailableError

    monkeypatch.setattr(gateway, "refund_payment", outage)

    body = await add_card(client, auth, gateway)

    assert body["status"] == "verified"
    assert body["refund_status"] == "pending"


async def test_gateway_down_at_start_creates_nothing(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    gateway.unavailable = True

    response = await start(client, auth)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_gateway_unavailable"


# ---- Same card twice, limits


async def test_same_card_checked_twice_is_saved_once(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    first = await add_card(client, auth, gateway)
    token = next(iter(gateway.tokens.values()))

    # Checkout hands back the same token for the same card.
    verification = await started(client, auth)
    result = gateway.complete_checkout(verification["checkout"]["order_id"])
    payment = gateway.payments[result.payment_id]
    gateway.payments[result.payment_id] = replace(
        payment, customer_id=token.customer_id, token_id=token.id
    )
    second = await complete(client, auth, verification["id"], result.payment_id, result.signature)

    assert second.json()["status"] == "verified"
    assert second.json()["card"]["id"] == first["card"]["id"]
    assert len((await client.get(CARDS, headers=auth)).json()) == 1
    assert len(gateway.refunds) == 2
    assert token.id in gateway.tokens  # still saved: it belongs to the first card


async def test_daily_limit_on_card_checks(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    app.state.settings = make_settings(card_verifications_per_day=2)

    assert (await start(client, auth)).status_code == 201
    assert (await start(client, auth)).status_code == 201
    response = await start(client, auth)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "card_verification_limit"
    assert "Retry-After" in response.headers


async def test_saved_card_limit(
    app: FastAPI, client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    app.state.settings = make_settings(max_cards_per_user=1)
    await add_card(client, auth, gateway)

    response = await start(client, auth)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "card_limit_reached"


# ---- Default and remove


async def test_set_default_moves_the_default(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    first = (await add_card(client, auth, gateway))["card"]
    second = (await add_card(client, auth, gateway))["card"]
    assert (first["is_default"], second["is_default"]) == (True, False)

    response = await client.patch(f"{CARDS}/{second['id']}/default", headers=auth)

    assert response.status_code == 200
    cards = (await client.get(CARDS, headers=auth)).json()
    assert [(c["id"], c["is_default"]) for c in cards] == [
        (second["id"], True),
        (first["id"], False),
    ]


async def test_removing_the_default_card_deletes_its_token_and_promotes_another(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    first = (await add_card(client, auth, gateway))["card"]
    second = (await add_card(client, auth, gateway))["card"]
    assert len(gateway.tokens) == 2

    response = await client.delete(f"{CARDS}/{first['id']}", headers=auth)

    assert response.status_code == 204
    assert len(gateway.tokens) == 1
    cards = (await client.get(CARDS, headers=auth)).json()
    assert [(c["id"], c["is_default"]) for c in cards] == [(second["id"], True)]
    again = await client.delete(f"{CARDS}/{first['id']}", headers=auth)
    assert again.status_code == 404
    default = await client.patch(f"{CARDS}/{first['id']}/default", headers=auth)
    assert default.status_code == 404


async def test_remove_fails_safely_when_the_gateway_is_down(
    client: AsyncClient, auth: dict[str, str], gateway: FakePaymentGateway
) -> None:
    card = (await add_card(client, auth, gateway))["card"]
    gateway.unavailable = True

    response = await client.delete(f"{CARDS}/{card['id']}", headers=auth)

    assert response.status_code == 503
    assert len((await client.get(CARDS, headers=auth)).json()) == 1


# ---- One user can't touch another's cards


async def test_users_cannot_reach_each_others_cards_or_checks(
    client: AsyncClient,
    auth: dict[str, str],
    other_auth: dict[str, str],
    gateway: FakePaymentGateway,
) -> None:
    mine = await add_card(client, auth, gateway)
    pending = await started(client, auth)
    result = gateway.complete_checkout(pending["checkout"]["order_id"])

    assert (await client.get(CARDS, headers=other_auth)).json() == []
    assert (await client.get(f"{VERIFY}/{mine['id']}", headers=other_auth)).status_code == 404
    stolen = await complete(client, other_auth, pending["id"], result.payment_id, result.signature)
    assert stolen.status_code == 404
    card_url = f"{CARDS}/{mine['card']['id']}"
    assert (await client.patch(f"{card_url}/default", headers=other_auth)).status_code == 404
    assert (await client.delete(card_url, headers=other_auth)).status_code == 404
    assert len((await client.get(CARDS, headers=auth)).json()) == 1


async def test_same_idempotency_key_is_separate_per_user(
    client: AsyncClient, auth: dict[str, str], other_auth: dict[str, str]
) -> None:
    key = new_key()

    mine = (await start(client, auth, key)).json()
    theirs = (await start(client, other_auth, key)).json()

    assert mine["id"] != theirs["id"]
    assert mine["checkout"]["customer_id"] != theirs["checkout"]["customer_id"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", CARDS),
        ("POST", VERIFY),
        ("GET", f"{VERIFY}/{uuid.uuid4()}"),
        ("DELETE", f"{CARDS}/{uuid.uuid4()}"),
    ],
)
async def test_cards_need_sign_in(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, path, headers={"Idempotency-Key": new_key()})

    assert response.status_code == 401
