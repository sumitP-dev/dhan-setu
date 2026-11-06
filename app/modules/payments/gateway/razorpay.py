"""Razorpay REST client (https://razorpay.com/docs/api/).

Safety rules:
- The API host is fixed here, not configurable, and redirects are never followed.
- Every id is shape-checked before it goes into a URL path.
- Responses are parsed strictly: amounts must be integers, and ids, amounts and currency
  must match what we asked for. Anything unexpected is an error, never a guess.
- POSTs are never retried here: a retried create_order could create a second order.
- Secrets and signatures are never logged.
"""

import logging
import re
from typing import Any, NoReturn

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from app.common.enums import CardNetwork, CardType, Currency
from app.modules.payments.gateway.base import (
    GatewayCard,
    GatewayCardToken,
    GatewayNotFoundError,
    GatewayOrder,
    GatewayPayment,
    GatewayPaymentStatus,
    GatewayRefund,
    GatewayRefundStatus,
    InvalidGatewayIdError,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
    require_gateway_id,
    validate_order_request,
)
from app.modules.payments.gateway.signatures import (
    hmac_sha256_hex,
    payment_signature,
    signature_matches,
)

logger = logging.getLogger(__name__)

RAZORPAY_API_BASE_URL = "https://api.razorpay.com/v1"
_MAX_CUSTOMER_NAME = 50

_NETWORKS = {
    "visa": CardNetwork.VISA,
    "mastercard": CardNetwork.MASTERCARD,
    "rupay": CardNetwork.RUPAY,
    "americanexpress": CardNetwork.AMEX,
    "amex": CardNetwork.AMEX,
    "dinersclub": CardNetwork.DINERS,
    "diners": CardNetwork.DINERS,
    "maestro": CardNetwork.MAESTRO,
    "jcb": CardNetwork.JCB,
}


def _card_network(value: str | None) -> CardNetwork:
    key = re.sub(r"[^a-z]", "", (value or "").lower())
    return _NETWORKS.get(key, CardNetwork.UNKNOWN)


def _card_type(value: str | None) -> CardType:
    try:
        return CardType((value or "").lower())
    except ValueError:
        return CardType.UNKNOWN


# ---- Response shapes. Only the fields we use; amounts must be real integers.


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Order(_Body):
    id: str
    amount: StrictInt
    currency: str
    receipt: str | None = None


class _Card(_Body):
    last4: str = Field(pattern=r"^[0-9]{4}$")
    network: str | None = None
    type: str | None = None
    issuer: str | None = None
    # Token responses send these as strings ("12", "2030").
    expiry_month: int | None = Field(default=None, ge=1, le=12)
    expiry_year: int | None = Field(default=None, ge=2000, le=2100)

    def to_card(self) -> GatewayCard:
        return GatewayCard(
            last4=self.last4,
            network=_card_network(self.network),
            card_type=_card_type(self.type),
            issuer=self.issuer,
            expiry_month=self.expiry_month,
            expiry_year=self.expiry_year,
        )


class _Payment(_Body):
    id: str
    order_id: str | None = None
    amount: StrictInt
    currency: str
    status: GatewayPaymentStatus
    method: str
    card: _Card | None = None
    customer_id: str | None = None
    token_id: str | None = None
    error_code: str | None = None
    error_description: str | None = None


class _Refund(_Body):
    id: str
    payment_id: str
    amount: StrictInt
    status: GatewayRefundStatus


class _Customer(_Body):
    id: str


class _Token(_Body):
    id: str
    method: str
    card: _Card


def _response_id(value: str, prefix: str) -> str:
    """An id Razorpay sent us. A bad one is the gateway's fault, not the client's."""
    try:
        return require_gateway_id(value, prefix)
    except InvalidGatewayIdError:
        logger.error("Razorpay returned a malformed %s id", prefix)
        raise PaymentGatewayError from None


def _currency(value: str) -> Currency:
    try:
        return Currency(value)
    except ValueError:
        raise PaymentGatewayError from None


class RazorpayGateway:
    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        webhook_secret: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._client = httpx.AsyncClient(
            base_url=RAZORPAY_API_BASE_URL,
            auth=httpx.BasicAuth(key_id, key_secret),
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "application/json"},
            transport=transport,  # tests pass a MockTransport
        )

    @property
    def key_id(self) -> str:
        return self._key_id

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- HTTP

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.TimeoutException:
            logger.warning("Razorpay %s %s timed out", method, path)
            raise PaymentGatewayUnavailableError from None
        except httpx.HTTPError as exc:
            logger.warning("Razorpay %s %s failed: %s", method, path, type(exc).__name__)
            raise PaymentGatewayUnavailableError from None

        if response.status_code >= 500:  # noqa: PLR2004
            logger.warning("Razorpay %s %s returned %s", method, path, response.status_code)
            raise PaymentGatewayUnavailableError

        body = self._json(response)
        if response.status_code >= 300:  # noqa: PLR2004
            self._raise_for_error(method, path, response.status_code, body)
        if body is None:
            logger.error("Razorpay %s %s returned a non-JSON body", method, path)
            raise PaymentGatewayError
        return body

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def _raise_for_error(
        method: str, path: str, status_code: int, body: dict[str, Any] | None
    ) -> NoReturn:
        error = (body or {}).get("error")
        error = error if isinstance(error, dict) else {}
        code = str(error.get("code", ""))
        description = str(error.get("description", ""))
        if status_code == 401:  # noqa: PLR2004
            logger.error("Razorpay rejected our API keys (%s %s)", method, path)
        else:
            logger.warning(
                "Razorpay %s %s returned %s: %s %s", method, path, status_code, code, description
            )
        if status_code == 404 or "does not exist" in description.lower():  # noqa: PLR2004
            raise GatewayNotFoundError
        raise PaymentGatewayError

    @staticmethod
    def _parse[T: _Body](model: type[T], body: dict[str, Any]) -> T:
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            logger.error("Unexpected Razorpay response for %s: %s", model.__name__, exc)
            raise PaymentGatewayError from None

    def _to_payment(self, body: dict[str, Any], expected_id: str) -> GatewayPayment:
        payment = self._parse(_Payment, body)
        if payment.id != expected_id or not payment.order_id:
            logger.error("Razorpay returned payment %s for %s", payment.id, expected_id)
            raise PaymentGatewayError
        return GatewayPayment(
            id=payment.id,
            order_id=payment.order_id,
            amount_paise=payment.amount,
            currency=_currency(payment.currency),
            status=payment.status,
            method=payment.method,
            card=payment.card.to_card() if payment.card else None,
            customer_id=payment.customer_id,
            token_id=payment.token_id,
            error_code=payment.error_code,
            error_description=payment.error_description,
        )

    # ---- API

    async def create_customer(self, *, name: str, email: str, phone: str) -> str:
        body = await self._request(
            "POST",
            "/customers",
            json={
                "name": name[:_MAX_CUSTOMER_NAME],
                "email": email,
                "contact": phone,
                # Return the existing customer instead of failing when they already exist.
                "fail_existing": "0",
            },
        )
        customer = self._parse(_Customer, body)
        return _response_id(customer.id, "cust")

    async def create_order(
        self, *, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder:
        validate_order_request(amount_paise, receipt, notes)
        body = await self._request(
            "POST",
            "/orders",
            json={
                "amount": amount_paise,
                "currency": Currency.INR.value,
                "receipt": receipt,
                "notes": notes,
            },
        )
        order = self._parse(_Order, body)
        if order.amount != amount_paise or order.currency != Currency.INR:
            logger.error("Razorpay order %s does not match the requested amount", order.id)
            raise PaymentGatewayError
        return GatewayOrder(
            id=_response_id(order.id, "order"),
            amount_paise=order.amount,
            currency=Currency.INR,
            receipt=receipt,
        )

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        require_gateway_id(payment_id, "pay")
        body = await self._request("GET", f"/payments/{payment_id}", params={"expand[]": "card"})
        return self._to_payment(body, payment_id)

    async def capture_payment(self, payment_id: str, amount_paise: int) -> GatewayPayment:
        require_gateway_id(payment_id, "pay")
        body = await self._request(
            "POST",
            f"/payments/{payment_id}/capture",
            json={"amount": amount_paise, "currency": Currency.INR.value},
        )
        return self._to_payment(body, payment_id)

    async def refund_payment(
        self, payment_id: str, amount_paise: int, *, receipt: str
    ) -> GatewayRefund:
        require_gateway_id(payment_id, "pay")
        validate_order_request(amount_paise, receipt, {})
        body = await self._request(
            "POST",
            f"/payments/{payment_id}/refund",
            json={"amount": amount_paise, "speed": "normal", "receipt": receipt},
        )
        refund = self._parse(_Refund, body)
        if refund.payment_id != payment_id or refund.amount != amount_paise:
            logger.error("Razorpay refund %s does not match payment %s", refund.id, payment_id)
            raise PaymentGatewayError
        return GatewayRefund(
            id=_response_id(refund.id, "rfnd"),
            payment_id=payment_id,
            amount_paise=refund.amount,
            status=refund.status,
        )

    async def fetch_card_token(self, customer_id: str, token_id: str) -> GatewayCardToken:
        require_gateway_id(customer_id, "cust")
        require_gateway_id(token_id, "token")
        body = await self._request("GET", f"/customers/{customer_id}/tokens/{token_id}")
        token = self._parse(_Token, body)
        if token.id != token_id or token.method != "card":
            logger.error("Razorpay returned token %s for %s", token.id, token_id)
            raise PaymentGatewayError
        return GatewayCardToken(id=token.id, customer_id=customer_id, card=token.card.to_card())

    async def delete_card_token(self, customer_id: str, token_id: str) -> None:
        require_gateway_id(customer_id, "cust")
        require_gateway_id(token_id, "token")
        await self._request("DELETE", f"/customers/{customer_id}/tokens/{token_id}")

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        expected = payment_signature(self._key_secret, order_id, payment_id)
        return signature_matches(expected, signature)

    def verify_webhook_signature(self, body: bytes, signature: str | None) -> bool:
        return signature_matches(hmac_sha256_hex(self._webhook_secret, body), signature)
