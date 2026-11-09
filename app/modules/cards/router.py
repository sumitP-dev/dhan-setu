import uuid

from fastapi import APIRouter, Response, status

from app.common.idempotency import IdempotencyKey
from app.common.schemas import error_responses
from app.modules.auth.dependencies import CurrentUser
from app.modules.cards.dependencies import CardServiceDep
from app.modules.cards.schemas import (
    CardRead,
    CardVerificationComplete,
    CardVerificationRead,
    CardVerificationStarted,
)
from app.modules.cards.service import FAILURE_MESSAGES, VerificationResult
from app.modules.payments.dependencies import PaymentGatewayDep
from app.modules.payments.schemas import CheckoutOptions

router = APIRouter(
    prefix="/cards",
    tags=["cards"],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)

_GATEWAY_ERRORS = (status.HTTP_502_BAD_GATEWAY, status.HTTP_503_SERVICE_UNAVAILABLE)


def _verification_read(result: VerificationResult) -> CardVerificationRead:
    verification = result.verification
    code = verification.failure_code
    return CardVerificationRead(
        id=verification.id,
        status=verification.status,
        amount_paise=verification.amount_paise,
        failure_code=code,
        failure_message=FAILURE_MESSAGES.get(code) if code else None,
        refund_status=verification.refund_status,
        card=CardRead.model_validate(result.card) if result.card else None,
        completed_at=verification.completed_at,
    )


@router.get("")
async def list_cards(user: CurrentUser, service: CardServiceDep) -> list[CardRead]:
    """Saved cards, default first. Only these can be used to pay."""
    return [CardRead.model_validate(card) for card in await service.list_cards(user)]


@router.post(
    "/verifications",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        *_GATEWAY_ERRORS,
    ),
)
async def start_card_verification(
    user: CurrentUser,
    service: CardServiceDep,
    gateway: PaymentGatewayDep,
    idempotency_key: IdempotencyKey,
) -> CardVerificationStarted:
    """Add a card: starts a ₹1 charge (refunded straight away). Open checkout with the
    returned options and save: 1, then call /complete with checkout's result."""
    verification = await service.start_verification(user, idempotency_key)
    return CardVerificationStarted(
        id=verification.id,
        status=verification.status,
        amount_paise=verification.amount_paise,
        expires_at=verification.expires_at,
        checkout=CheckoutOptions(
            key_id=gateway.key_id,
            order_id=verification.gateway_order_id,
            customer_id=verification.gateway_customer_id,
        ),
    )


@router.get(
    "/verifications/{verification_id}",
    responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def get_card_verification(
    verification_id: uuid.UUID, user: CurrentUser, service: CardServiceDep
) -> CardVerificationRead:
    return _verification_read(await service.get_verification(user, verification_id))


@router.post(
    "/verifications/{verification_id}/complete",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        *_GATEWAY_ERRORS,
    ),
)
async def complete_card_verification(
    verification_id: uuid.UUID,
    body: CardVerificationComplete,
    user: CurrentUser,
    service: CardServiceDep,
) -> CardVerificationRead:
    """Send checkout's razorpay_payment_id and razorpay_signature. Safe to repeat."""
    result = await service.complete_verification(
        user, verification_id, payment_id=body.payment_id, signature=body.signature
    )
    return _verification_read(result)


@router.patch(
    "/{card_id}/default",
    responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def set_default_card(
    card_id: uuid.UUID, user: CurrentUser, service: CardServiceDep
) -> CardRead:
    return CardRead.model_validate(await service.set_default(user, card_id))


@router.delete(
    "/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(
        status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT, *_GATEWAY_ERRORS
    ),
)
async def remove_card(card_id: uuid.UUID, user: CurrentUser, service: CardServiceDep) -> Response:
    """Removes the card here and its saved token at the gateway."""
    await service.remove_card(user, card_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
