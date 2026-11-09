from typing import Annotated

from fastapi import Depends, Request

from app.core.config import AppSettings
from app.core.database import DbSession
from app.modules.cards.repository import CardRepository
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.repository import PaymentRepository
from app.modules.payments.service import PaymentService


def get_payment_gateway(request: Request) -> PaymentGateway:
    gateway: PaymentGateway = request.app.state.payment_gateway
    return gateway


PaymentGatewayDep = Annotated[PaymentGateway, Depends(get_payment_gateway)]


def get_payment_service(
    db: DbSession, gateway: PaymentGatewayDep, settings: AppSettings
) -> PaymentService:
    return PaymentService(
        payments=PaymentRepository(db),
        cards=CardRepository(db),
        gateway=gateway,
        settings=settings,
    )


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]
