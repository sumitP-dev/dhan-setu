from fastapi import FastAPI

from app.main import create_app
from app.modules.payments.gateway.fake import FakePaymentGateway
from app.modules.payments.gateway.razorpay import RazorpayGateway
from tests.helpers import make_settings


def test_app_uses_the_fake_gateway_by_default(app: FastAPI) -> None:
    assert isinstance(app.state.payment_gateway, FakePaymentGateway)


def test_app_uses_razorpay_when_configured() -> None:
    settings = make_settings(
        payment_gateway="razorpay",
        razorpay_key_id="rzp_test_abc123",
        razorpay_key_secret="key-secret-value",
        razorpay_webhook_secret="webhook-secret-value",
    )

    gateway = create_app(settings).state.payment_gateway

    assert isinstance(gateway, RazorpayGateway)
    assert gateway.key_id == "rzp_test_abc123"
