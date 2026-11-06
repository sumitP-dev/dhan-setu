import logging
import uuid

from app.common.exceptions import ConflictError
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.models import GatewayCustomer
from app.modules.payments.repository import GatewayCustomerRepository
from app.modules.users.models import User

logger = logging.getLogger(__name__)


class GatewayCustomerService:
    def __init__(self, *, customers: GatewayCustomerRepository, gateway: PaymentGateway) -> None:
        self._customers = customers
        self._gateway = gateway

    async def get_or_create(self, user: User) -> str:
        """The user's gateway customer id, created on first use."""
        existing = await self._customers.get_for_user(user.id)
        if existing is not None:
            return existing.gateway_customer_id

        # The gateway returns the same customer when asked twice, so a race between two
        # requests ends with both holding the same id; only one row is inserted.
        customer_id = await self._gateway.create_customer(
            name=user.full_name, email=user.email, phone=user.phone
        )
        if await self._customers.add_if_absent(
            GatewayCustomer(user_id=user.id, gateway_customer_id=customer_id)
        ):
            return customer_id

        existing = await self._customers.get_for_user(user.id)
        if existing is None or existing.gateway_customer_id != customer_id:
            # The gateway matched this user to a customer that belongs to someone else.
            logger.error("Gateway customer for user %s is already linked elsewhere", user.id)
            raise ConflictError(
                "We couldn't set up card payments for this account. Contact support.",
                code="gateway_customer_conflict",
            )
        return customer_id

    async def lock(self, user_id: uuid.UUID) -> None:
        """Lock the user's customer row until the transaction ends, so card changes for
        one user (save, set default, remove) happen one at a time."""
        await self._customers.get_for_user(user_id, for_update=True)
