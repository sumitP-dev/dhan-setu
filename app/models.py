"""Import every module's models here so Alembic autogenerate can see them."""

from app.core.database import Base
from app.modules.admin.models import AdminAuditLog
from app.modules.admin_auth.models import AdminSession
from app.modules.auth.models import OtpCode, RefreshToken
from app.modules.cards.models import Card, CardVerification
from app.modules.kyc.models import (
    KycAccessLog,
    KycDigiLockerSession,
    KycEvent,
    KycProfile,
    KycSelfieSession,
)
from app.modules.payments.models import GatewayCustomer, Payment, PaymentEvent
from app.modules.users.models import User
from app.modules.webhooks.models import WebhookEvent

__all__ = [
    "AdminAuditLog",
    "AdminSession",
    "Base",
    "Card",
    "CardVerification",
    "GatewayCustomer",
    "KycAccessLog",
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
