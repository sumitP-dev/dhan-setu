from typing import Annotated

from fastapi import Depends

from app.core.config import AppSettings
from app.core.database import DbSession
from app.modules.cards.repository import CardRepository, CardVerificationRepository
from app.modules.cards.service import CardService
from app.modules.payments.dependencies import PaymentGatewayDep
from app.modules.payments.repository import GatewayCustomerRepository
from app.modules.payments.service import GatewayCustomerService


def get_card_service(
    db: DbSession, gateway: PaymentGatewayDep, settings: AppSettings
) -> CardService:
    return CardService(
        cards=CardRepository(db),
        verifications=CardVerificationRepository(db),
        customers=GatewayCustomerService(customers=GatewayCustomerRepository(db), gateway=gateway),
        gateway=gateway,
        settings=settings,
    )


CardServiceDep = Annotated[CardService, Depends(get_card_service)]
