from enum import StrEnum


class Currency(StrEnum):
    INR = "INR"


class PaymentMethod(StrEnum):
    CARD = "card"
    UPI = "upi"
    BBPS = "bbps"


class PaymentStatus(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"


class CardNetwork(StrEnum):
    VISA = "visa"
    MASTERCARD = "mastercard"
    RUPAY = "rupay"
    AMEX = "amex"
    DINERS = "diners"


class CardType(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class UserRole(StrEnum):
    CUSTOMER = "customer"
    MERCHANT = "merchant"
    ADMIN = "admin"


class UserStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"  # registered, mobile not verified yet
    ACTIVE = "active"
    BLOCKED = "blocked"


class OtpPurpose(StrEnum):
    VERIFY_PHONE = "verify_phone"
    LOGIN = "login"
    RESET_PASSWORD = "reset_password"  # noqa: S105  (reserved; no endpoint uses it yet)
