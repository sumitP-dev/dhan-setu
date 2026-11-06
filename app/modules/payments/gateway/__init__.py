from app.core.config import Settings
from app.modules.payments.gateway.base import PaymentGateway
from app.modules.payments.gateway.fake import FakePaymentGateway
from app.modules.payments.gateway.razorpay import RazorpayGateway


def create_payment_gateway(settings: Settings) -> PaymentGateway:
    """Settings validation has already checked the keys exist and match the environment."""
    if settings.payment_gateway == "fake":
        return FakePaymentGateway()
    key_id = settings.razorpay_key_id
    key_secret = settings.razorpay_key_secret
    webhook_secret = settings.razorpay_webhook_secret
    if not key_id or key_secret is None or webhook_secret is None:
        msg = "Razorpay keys are missing"
        raise RuntimeError(msg)
    return RazorpayGateway(
        key_id=key_id,
        key_secret=key_secret.get_secret_value(),
        webhook_secret=webhook_secret.get_secret_value(),
        timeout_seconds=settings.razorpay_timeout_seconds,
    )


__all__ = ["PaymentGateway", "create_payment_gateway"]
