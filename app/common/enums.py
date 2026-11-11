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
    MAESTRO = "maestro"
    JCB = "jcb"
    UNKNOWN = "unknown"


class CardType(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    PREPAID = "prepaid"
    UNKNOWN = "unknown"


class UserRole(StrEnum):
    CUSTOMER = "customer"
    MERCHANT = "merchant"
    ADMIN = "admin"


class UserStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"  # registered, mobile not verified yet
    ACTIVE = "active"
    BLOCKED = "blocked"


class KycStatus(StrEnum):
    NOT_SUBMITTED = "not_submitted"  # not started, or skipped for later
    IN_PROGRESS = "in_progress"  # documents fetched, not submitted yet
    PENDING_REVIEW = "pending_review"  # submitted, waiting for an admin
    VERIFIED = "verified"
    REJECTED = "rejected"  # can try again


class OtpPurpose(StrEnum):
    VERIFY_PHONE = "verify_phone"
    LOGIN = "login"
    RESET_PASSWORD = "reset_password"  # noqa: S105  (reserved; no endpoint uses it yet)


class CardStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


class CardVerificationStatus(StrEnum):
    CREATED = "created"  # ₹1 order made, waiting for the user to pay
    CHARGED = "charged"  # ₹1 taken, card not saved yet (gateway hiccup); completing again resumes
    VERIFIED = "verified"
    FAILED = "failed"


class RefundStatus(StrEnum):
    PENDING = "pending"  # not confirmed by the gateway yet; a reconciliation job retries it
    REQUESTED = "requested"  # the gateway accepted it and is processing
    PROCESSED = "processed"
    FAILED = "failed"


class WebhookEventStatus(StrEnum):
    PROCESSED = "processed"
    IGNORED = "ignored"  # an event type or order we don't handle
    FAILED = "failed"  # signed, but unusable (bad payload, doesn't match our records)
