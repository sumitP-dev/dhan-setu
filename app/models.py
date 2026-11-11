"""Import every module's models here so Alembic autogenerate can see them."""

from app.core.database import Base
from app.modules.auth.models import OtpCode, RefreshToken
from app.modules.cards.models import Card, CardVerification
from app.modules.kyc.models import KycDigiLockerSession, KycEvent, KycProfile, KycSelfieSession
from app.modules.payments.models import GatewayCustomer, Payment, PaymentEvent
from app.modules.users.models import User
from app.modules.webhooks.models import WebhookEvent

__all__ = [
    "Base",
    "Card",
    "CardVerification",
    "GatewayCustomer",
    "KycDigiLockerSession",
    "KycEvent",
    "KycProfile",
    "KycSelfieSession",
    "OtpCode",
    "Payment",
    "PaymentEvent",
    "RefreshToken",
    "User",
    "WebhookEvent",
]
