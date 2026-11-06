"""RazorpayGateway against a fake HTTP transport: no network, real request/response code."""

import base64
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.common.enums import CardNetwork, CardType
from app.modules.payments.gateway.base import (
    GatewayNotFoundError,
    GatewayPaymentStatus,
    InvalidGatewayIdError,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
)
from app.modules.payments.gateway.razorpay import RazorpayGateway
from app.modules.payments.gateway.signatures import hmac_sha256_hex, payment_signature

KEY_ID = "rzp_test_abc123"
KEY_SECRET = "key-secret-value-xyz"
WEBHOOK_SECRET = "webhook-secret-value-xyz"

Handler = Callable[[httpx.Request], httpx.Response]


def make_gateway(handler: Handler, seen: list[httpx.Request] | None = None) -> RazorpayGateway:
    def record(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return RazorpayGateway(
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
        webhook_secret=WEBHOOK_SECRET,
        timeout_seconds=5,
        transport=httpx.MockTransport(record),
    )


def respond(status: int, body: Any) -> Handler:
    return lambda _: httpx.Response(status, json=body)


def payment_body(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "pay_29QQoUBi66xm2f",
        "entity": "payment",
        "order_id": "order_9A33XWu170gUtm",
        "amount": 50_000,
        "currency": "INR",
        "status": "captured",
        "method": "card",
        "card": {"last4": "4242", "network": "MasterCard", "type": "debit", "issuer": "HDFC"},
        "customer_id": "cust_1Aa00000000001",
        "token_id": "token_4lsdksD31GaZ09",
    } | overrides


async def test_create_order_sends_paise_with_basic_auth() -> None:
    seen: list[httpx.Request] = []
    gateway = make_gateway(
        respond(
            200,
            {"id": "order_9A33XWu170gUtm", "amount": 50_000, "currency": "INR", "receipt": "r1"},
        ),
        seen,
    )

    order = await gateway.create_order(amount_paise=50_000, receipt="r1", notes={"p": "1"})

    assert order.id == "order_9A33XWu170gUtm"
    assert order.amount_paise == 50_000
    request = seen[0]
    assert str(request.url) == "https://api.razorpay.com/v1/orders"
    assert json.loads(request.content) == {
        "amount": 50_000,
        "currency": "INR",
        "receipt": "r1",
        "notes": {"p": "1"},
    }
    expected_auth = base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected_auth}"


@pytest.mark.parametrize(
    "body",
    [
        {"id": "order_9A33XWu170gUtm", "amount": 1, "currency": "INR"},  # different amount
        {"id": "order_9A33XWu170gUtm", "amount": 50_000, "currency": "USD"},
        {"id": "order_9A33XWu170gUtm", "amount": 500.0, "currency": "INR"},  # not an integer
        {"id": "order_9A33XWu170gUtm", "amount": "50000", "currency": "INR"},
        {"id": "../evil", "amount": 50_000, "currency": "INR"},
        {"amount": 50_000, "currency": "INR"},
    ],
)
async def test_unexpected_order_responses_are_refused(body: dict[str, Any]) -> None:
    gateway = make_gateway(respond(200, body))

    with pytest.raises(PaymentGatewayError):
        await gateway.create_order(amount_paise=50_000, receipt="r1", notes={})


async def test_fetch_payment_parses_card_details() -> None:
    seen: list[httpx.Request] = []
    gateway = make_gateway(respond(200, payment_body()), seen)

    payment = await gateway.fetch_payment("pay_29QQoUBi66xm2f")

    assert seen[0].url.path == "/v1/payments/pay_29QQoUBi66xm2f"
    assert seen[0].url.params["expand[]"] == "card"
    assert payment.status == GatewayPaymentStatus.CAPTURED
    assert payment.amount_paise == 50_000
    assert payment.card is not None
    assert payment.card.network == CardNetwork.MASTERCARD
    assert payment.card.card_type == CardType.DEBIT
    assert payment.token_id == "token_4lsdksD31GaZ09"


async def test_fetch_payment_refuses_a_different_payment() -> None:
    gateway = make_gateway(respond(200, payment_body(id="pay_SomeoneElse0001")))

    with pytest.raises(PaymentGatewayError):
        await gateway.fetch_payment("pay_29QQoUBi66xm2f")


@pytest.mark.parametrize(
    "payment_id",
    ["../orders", "pay_29QQoUBi66xm2f/capture", "order_9A33XWu170gUtm", "pay_", "pay_a?x=1", ""],
)
async def test_bad_ids_never_reach_the_network(payment_id: str) -> None:
    seen: list[httpx.Request] = []
    gateway = make_gateway(respond(200, payment_body()), seen)

    with pytest.raises(InvalidGatewayIdError):
        await gateway.fetch_payment(payment_id)
    assert seen == []


async def test_card_token_is_parsed_with_expiry() -> None:
    gateway = make_gateway(
        respond(
            200,
            {
                "id": "token_4lsdksD31GaZ09",
                "method": "card",
                "card": {
                    "last4": "1111",
                    "network": "RuPay",
                    "type": "credit",
                    "expiry_month": "07",
                    "expiry_year": "2031",
                },
            },
        )
    )

    token = await gateway.fetch_card_token("cust_1Aa00000000001", "token_4lsdksD31GaZ09")

    assert token.card.network == CardNetwork.RUPAY
    assert (token.card.expiry_month, token.card.expiry_year) == (7, 2031)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (400, {"error": {"description": "The id provided does not exist"}}, GatewayNotFoundError),
        (404, {}, GatewayNotFoundError),
        (400, {"error": {"code": "BAD_REQUEST_ERROR", "description": "bad"}}, PaymentGatewayError),
        (401, {"error": {"code": "BAD_REQUEST_ERROR"}}, PaymentGatewayError),
        (302, {}, PaymentGatewayError),
        (500, {}, PaymentGatewayUnavailableError),
        (503, "not json", PaymentGatewayUnavailableError),
    ],
)  # fmt: skip
async def test_http_errors_are_mapped(status: int, body: Any, error: type[Exception]) -> None:
    gateway = make_gateway(respond(status, body))

    with pytest.raises(error):
        await gateway.fetch_payment("pay_29QQoUBi66xm2f")


async def test_redirects_are_not_followed() -> None:
    seen: list[httpx.Request] = []
    gateway = make_gateway(
        lambda _: httpx.Response(302, headers={"Location": "https://evil.example/"}), seen
    )

    with pytest.raises(PaymentGatewayError):
        await gateway.fetch_payment("pay_29QQoUBi66xm2f")
    assert len(seen) == 1


async def test_timeouts_and_network_errors_mean_unavailable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler in (timeout, refused):
        with pytest.raises(PaymentGatewayUnavailableError):
            await make_gateway(handler).fetch_payment("pay_29QQoUBi66xm2f")


async def test_create_order_is_not_retried_after_a_timeout() -> None:
    seen: list[httpx.Request] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(PaymentGatewayUnavailableError):
        await make_gateway(timeout, seen).create_order(amount_paise=100, receipt="r", notes={})
    assert len(seen) == 1


def test_signatures_use_the_right_secrets() -> None:
    gateway = make_gateway(respond(200, {}))
    good = payment_signature(KEY_SECRET, "order_9A33XWu170gUtm", "pay_29QQoUBi66xm2f")

    assert gateway.verify_payment_signature("order_9A33XWu170gUtm", "pay_29QQoUBi66xm2f", good)
    assert not gateway.verify_payment_signature("order_9A33XWu170gUtm", "pay_OtherPayment01", good)
    body = b'{"event":"payment.captured"}'
    assert gateway.verify_webhook_signature(body, hmac_sha256_hex(WEBHOOK_SECRET, body))
    assert not gateway.verify_webhook_signature(body, hmac_sha256_hex(KEY_SECRET, body))
    assert not gateway.verify_webhook_signature(body, None)


async def test_secrets_never_appear_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for status in (400, 401, 500):
        with pytest.raises(PaymentGatewayError):
            await make_gateway(respond(status, {})).fetch_payment("pay_29QQoUBi66xm2f")

    assert KEY_SECRET not in caplog.text
    assert base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode() not in caplog.text


async def test_refund_sends_amount_and_checks_the_reply() -> None:
    seen: list[httpx.Request] = []
    reply = {
        "id": "rfnd_FP8QHiV938haTz",
        "payment_id": "pay_29QQoUBi66xm2f",
        "amount": 100,
        "status": "processed",
    }
    gateway = make_gateway(respond(200, reply), seen)

    refund = await gateway.refund_payment("pay_29QQoUBi66xm2f", 100, receipt="cvr_1")

    assert refund.id == "rfnd_FP8QHiV938haTz"
    assert seen[0].url.path == "/v1/payments/pay_29QQoUBi66xm2f/refund"
    assert json.loads(seen[0].content) == {"amount": 100, "speed": "normal", "receipt": "cvr_1"}

    for bad in ({"amount": 99}, {"payment_id": "pay_SomeoneElse0001"}, {"id": "nope"}):
        with pytest.raises(PaymentGatewayError):
            await make_gateway(respond(200, reply | bad)).refund_payment(
                "pay_29QQoUBi66xm2f", 100, receipt="cvr_1"
            )
