import pytest

from app.modules.payments.gateway.base import (
    GatewayNotFoundError,
    GatewayPaymentStatus,
    InvalidGatewayIdError,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
)
from app.modules.payments.gateway.fake import FakePaymentGateway


async def test_checkout_signature_verifies_and_tampering_fails() -> None:
    gateway = FakePaymentGateway()
    order = await gateway.create_order(amount_paise=50_000, receipt="r1", notes={})

    result = gateway.complete_checkout(order.id)

    assert gateway.verify_payment_signature(order.id, result.payment_id, result.signature)
    assert not gateway.verify_payment_signature(order.id, result.payment_id, "0" * 64)
    other = await gateway.create_order(amount_paise=100, receipt="r2", notes={})
    assert not gateway.verify_payment_signature(other.id, result.payment_id, result.signature)


async def test_signatures_from_another_instance_fail() -> None:
    """Each process has its own secret, so a signature can't be reused elsewhere."""
    gateway, other = FakePaymentGateway(), FakePaymentGateway()
    order = await gateway.create_order(amount_paise=100, receipt="r", notes={})
    result = gateway.complete_checkout(order.id)

    assert not other.verify_payment_signature(order.id, result.payment_id, result.signature)


async def test_saved_card_creates_a_token_for_that_customer_only() -> None:
    gateway = FakePaymentGateway()
    customer = await gateway.create_customer(name="A", email="a@example.com", phone="+919876543210")
    order = await gateway.create_order(amount_paise=100, receipt="r", notes={})

    result = gateway.complete_checkout(order.id, customer_id=customer, save_card=True)
    payment = await gateway.fetch_payment(result.payment_id)

    assert payment.token_id is not None
    token = await gateway.fetch_card_token(customer, payment.token_id)
    assert token.card.last4 == "1111"
    stranger = await gateway.create_customer(name="B", email="b@example.com", phone="+919000000000")
    with pytest.raises(GatewayNotFoundError):
        await gateway.fetch_card_token(stranger, payment.token_id)


async def test_capture_needs_an_authorized_payment_and_the_same_amount() -> None:
    gateway = FakePaymentGateway()
    order = await gateway.create_order(amount_paise=1_000, receipt="r", notes={})
    result = gateway.complete_checkout(order.id, status=GatewayPaymentStatus.AUTHORIZED)

    with pytest.raises(PaymentGatewayError):
        await gateway.capture_payment(result.payment_id, 999)
    captured = await gateway.capture_payment(result.payment_id, 1_000)

    assert captured.status == GatewayPaymentStatus.CAPTURED
    with pytest.raises(PaymentGatewayError):
        await gateway.capture_payment(result.payment_id, 1_000)


@pytest.mark.parametrize("amount", [0, 99, -100, True])
async def test_orders_below_one_rupee_are_refused(amount: int) -> None:
    with pytest.raises(ValueError, match="amount_paise"):
        await FakePaymentGateway().create_order(amount_paise=amount, receipt="r", notes={})


async def test_bad_ids_and_outages() -> None:
    gateway = FakePaymentGateway()

    with pytest.raises(InvalidGatewayIdError):
        await gateway.fetch_payment("../orders")
    with pytest.raises(GatewayNotFoundError):
        await gateway.fetch_payment("pay_DoesNotExist01")
    gateway.unavailable = True
    with pytest.raises(PaymentGatewayUnavailableError):
        await gateway.create_order(amount_paise=100, receipt="r", notes={})
