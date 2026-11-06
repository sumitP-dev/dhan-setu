from typing import Annotated

from fastapi import Depends, Request

from app.modules.payments.gateway import PaymentGateway


def get_payment_gateway(request: Request) -> PaymentGateway:
    gateway: PaymentGateway = request.app.state.payment_gateway
    return gateway


PaymentGatewayDep = Annotated[PaymentGateway, Depends(get_payment_gateway)]
