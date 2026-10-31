"""Outgoing SMS. Real providers (MSG91, Twilio, ...) implement SmsSender later."""

import logging
from typing import Protocol

from app.common.enums import OtpPurpose

logger = logging.getLogger(__name__)

PHONE_VISIBLE_DIGITS = 4


def mask_phone(phone: str) -> str:
    """+919876543210 -> +91******3210"""
    hidden = len(phone) - 3 - PHONE_VISIBLE_DIGITS
    return phone[:3] + "*" * hidden + phone[-PHONE_VISIBLE_DIGITS:]


class SmsSender(Protocol):
    async def send_otp(self, phone: str, code: str, purpose: OtpPurpose) -> None: ...


class ConsoleSmsSender:
    """Development stub: writes the OTP to the server log instead of sending an SMS.

    log_codes=False (used in production) logs only that a code was sent, because
    OTPs in production logs would let anyone with log access sign in as the user.
    """

    def __init__(self, *, log_codes: bool) -> None:
        self._log_codes = log_codes

    async def send_otp(self, phone: str, code: str, purpose: OtpPurpose) -> None:
        if self._log_codes:
            logger.info("[SMS stub] %s OTP for %s: %s", purpose, mask_phone(phone), code)
        else:
            logger.warning("[SMS stub] %s OTP for %s not delivered", purpose, mask_phone(phone))
