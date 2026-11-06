"""Import every module's models here so Alembic autogenerate can see them."""

from app.core.database import Base
from app.modules.auth.models import OtpCode, RefreshToken
from app.modules.cards.models import Card, CardVerification
from app.modules.payments.models import GatewayCustomer
from app.modules.users.models import User

__all__ = [
    "Base",
    "Card",
    "CardVerification",
    "GatewayCustomer",
    "OtpCode",
    "RefreshToken",
    "User",
]
