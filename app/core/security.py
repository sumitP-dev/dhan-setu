"""Password hashing (bcrypt), access tokens (JWT), refresh tokens (opaque) and OTPs."""

import asyncio
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import bcrypt
import jwt
from pydantic import SecretStr

from app.common.exceptions import UnauthorizedError
from app.common.validators import BCRYPT_MAX_PASSWORD_BYTES

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105  (a claim value, not a secret)
BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


# ---- Passwords
# bcrypt is deliberately slow (~0.25s), so it runs in a worker thread to keep the
# event loop free for other requests.


def _hash_password_sync(password: str, rounds: int) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds)).decode()


def _verify_password_sync(password: str, password_hash: str) -> bool:
    encoded = password.encode()
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(encoded, password_hash.encode())


@lru_cache
def _dummy_hash(rounds: int) -> str:
    return _hash_password_sync(secrets.token_urlsafe(16), rounds)


def _verify_dummy_sync(password: str, rounds: int) -> None:
    _verify_password_sync(password, _dummy_hash(rounds))


async def hash_password(password: str, *, rounds: int) -> str:
    return await asyncio.to_thread(_hash_password_sync, password, rounds)


async def verify_password(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(_verify_password_sync, password, password_hash)


async def burn_password_check(password: str, *, rounds: int) -> None:
    """Take as long as a real check when the account doesn't exist, so response
    timing can't be used to discover which emails are registered."""
    await asyncio.to_thread(_verify_dummy_sync, password, rounds)


# ---- Access tokens (JWT)


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    expires_at: datetime


def create_access_token(
    user_id: uuid.UUID,
    *,
    secret: SecretStr,
    ttl: timedelta,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": ACCESS_TOKEN_TYPE,
        "iat": issued_at,
        "exp": issued_at + ttl,
    }
    return jwt.encode(payload, secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def _invalid_token() -> UnauthorizedError:
    return UnauthorizedError("Invalid access token", code="invalid_token", headers=BEARER_CHALLENGE)


def decode_access_token(token: str, *, secret: SecretStr) -> AccessTokenClaims:
    """Raises UnauthorizedError: code "token_expired" (client should refresh) or
    "invalid_token" (client should log in again)."""
    try:
        payload = jwt.decode(
            token,
            secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "type", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError(
            "Access token expired", code="token_expired", headers=BEARER_CHALLENGE
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise _invalid_token() from exc

    if payload["type"] != ACCESS_TOKEN_TYPE:
        raise _invalid_token()
    try:
        user_id = uuid.UUID(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise _invalid_token() from exc
    return AccessTokenClaims(
        user_id=user_id, expires_at=datetime.fromtimestamp(payload["exp"], UTC)
    )


# ---- Refresh tokens
# Random and opaque. Only the SHA-256 hash is stored, so a database leak doesn't
# leak usable tokens. (No salt needed: the token itself has 256 bits of entropy.)


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- One-time codes (OTP)
# A 6-digit code has only a million values, so a plain SHA-256 could be reversed by
# trying them all. HMAC with the server secret means a database leak alone isn't enough.

OTP_DIGITS = 6


def generate_otp() -> str:
    return f"{secrets.randbelow(10**OTP_DIGITS):0{OTP_DIGITS}d}"


def hash_otp(code: str, *, phone: str, purpose: str, secret: SecretStr) -> str:
    # phone + purpose are mixed in so a code can't be replayed for another number or flow.
    message = f"{purpose}:{phone}:{code}".encode()
    return hmac.new(secret.get_secret_value().encode(), message, hashlib.sha256).hexdigest()


def otp_matches(code: str, code_hash: str, *, phone: str, purpose: str, secret: SecretStr) -> bool:
    expected = hash_otp(code, phone=phone, purpose=purpose, secret=secret)
    return hmac.compare_digest(expected, code_hash)
