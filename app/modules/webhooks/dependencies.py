from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.cards.dependencies import CardServiceDep
from app.modules.payments.dependencies import PaymentGatewayDep, PaymentServiceDep
from app.modules.webhooks.repository import WebhookEventRepository
from app.modules.webhooks.service import RazorpayWebhookService


def get_razorpay_webhook_service(
    db: DbSession,
    payments: PaymentServiceDep,
    cards: CardServiceDep,
    gateway: PaymentGatewayDep,
) -> RazorpayWebhookService:
    return RazorpayWebhookService(
        session=db,
        events=WebhookEventRepository(db),
        payments=payments,
        cards=cards,
        gateway=gateway,
    )


RazorpayWebhookServiceDep = Annotated[RazorpayWebhookService, Depends(get_razorpay_webhook_service)]
