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
