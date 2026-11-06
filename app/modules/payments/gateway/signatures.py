"""HMAC-SHA256 signatures used by Razorpay.

- Checkout result: hex(HMAC(key_secret, "<order_id>|<payment_id>"))
- Webhook:         hex(HMAC(webhook_secret, raw request body))
"""

import hashlib
import hmac
import re

_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")


def hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def payment_signature(secret: str, order_id: str, payment_id: str) -> str:
    return hmac_sha256_hex(secret, f"{order_id}|{payment_id}".encode())


def signature_matches(expected: str, received: str | None) -> bool:
    """Constant-time compare. Anything that isn't 64 lowercase hex chars is rejected
    up front (compare_digest also needs ASCII, so odd input can't raise)."""
    if not received or not _SIGNATURE_RE.fullmatch(received):
        return False
    return hmac.compare_digest(expected.encode(), received.encode())
