import uuid

from fastapi import APIRouter, status

from app.common.idempotency import IdempotencyKey
from app.common.schemas import error_responses
from app.modules.auth.dependencies import CurrentUser
from app.modules.payments.dependencies import PaymentGatewayDep, PaymentServiceDep
from app.modules.payments.schemas import (
    CheckoutOptions,
    PaymentCard,
    PaymentCreate,
    PaymentOrderCreated,
    PaymentRead,
    PaymentVerify,
)
from app.modules.payments.service import (
    PAYMENT_FAILURE_MESSAGES,
    NewPayment,
    PaymentResult,
)

router = APIRouter(
    prefix="/payments",
    tags=["payments"],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)

_GATEWAY_ERRORS = (status.HTTP_502_BAD_GATEWAY, status.HTTP_503_SERVICE_UNAVAILABLE)


def _payment_read(result: PaymentResult) -> PaymentRead:
    payment, card = result.payment, result.card
    code = payment.failure_code
    return PaymentRead(
        id=payment.id,
        status=payment.status,
        amount_paise=payment.amount_paise,
        currency=payment.currency,
        method=payment.method,
        description=payment.description,
        card=PaymentCard.model_validate(card) if card else None,
        failure_code=code,
        failure_message=PAYMENT_FAILURE_MESSAGES.get(code) if code else None,
        refund_status=payment.refund_status,
        created_at=payment.created_at,
        paid_at=payment.paid_at,
    )


@router.post(
    "/orders",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        *_GATEWAY_ERRORS,
    ),
)
async def create_payment_order(
    body: PaymentCreate,
    user: CurrentUser,
    service: PaymentServiceDep,
    gateway: PaymentGatewayDep,
    idempotency_key: IdempotencyKey,
) -> PaymentOrderCreated:
    """Start a card payment with a saved card. Retrying with the same Idempotency-Key and
    body returns the same payment; a different body with that key is refused (409)."""
    result = await service.create_payment(
        user,
        idempotency_key,
        NewPayment(
            amount_paise=body.amount_paise, card_id=body.card_id, description=body.description
        ),
    )
    payment = result.payment
    # The customer id comes from the chosen card, so checkout lists the user's saved cards.
    customer_id = result.card.gateway_customer_id if result.card else ""
    return PaymentOrderCreated(
        payment=_payment_read(result),
        checkout=CheckoutOptions(
            key_id=gateway.key_id, order_id=payment.gateway_order_id, customer_id=customer_id
        ),
    )


@router.post(
    "/{payment_id}/verify",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        *_GATEWAY_ERRORS,
    ),
)
async def verify_payment(
    payment_id: uuid.UUID, body: PaymentVerify, user: CurrentUser, service: PaymentServiceDep
) -> PaymentRead:
    """Send checkout's razorpay_payment_id and razorpay_signature. Safe to repeat."""
    result = await service.verify_payment(
        user, payment_id, gateway_payment_id=body.payment_id, signature=body.signature
    )
    return _payment_read(result)


@router.get(
    "/{payment_id}",
    responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def get_payment(
    payment_id: uuid.UUID, user: CurrentUser, service: PaymentServiceDep
) -> PaymentRead:
    """Payment status. Poll this while it's pending."""
    return _payment_read(await service.get_payment(user, payment_id))
