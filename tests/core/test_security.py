import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import SecretStr

from app.common.exceptions import UnauthorizedError
from app.core.security import (
    JWT_ALGORITHM,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from tests.helpers import TEST_JWT_SECRET

SECRET = SecretStr(TEST_JWT_SECRET)


async def test_password_hash_round_trip() -> None:
    password_hash = await hash_password("Secret123", rounds=4)

    assert password_hash != "Secret123"
    assert await verify_password("Secret123", password_hash)
    assert not await verify_password("Secret124", password_hash)


async def test_password_hashes_are_salted() -> None:
    assert await hash_password("Secret123", rounds=4) != await hash_password("Secret123", rounds=4)


async def test_overlong_password_never_verifies() -> None:
    password_hash = await hash_password("a" * 72, rounds=4)

    assert not await verify_password("a" * 73, password_hash)


def test_access_token_round_trip() -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, secret=SECRET, ttl=timedelta(minutes=15))

    claims = decode_access_token(token, secret=SECRET)

    assert claims.user_id == user_id


def test_expired_access_token() -> None:
    token = create_access_token(
        uuid.uuid4(),
        secret=SECRET,
        ttl=timedelta(minutes=15),
        now=datetime.now(UTC) - timedelta(hours=1),
    )

    with pytest.raises(UnauthorizedError) as exc_info:
        decode_access_token(token, secret=SECRET)

    assert exc_info.value.code == "token_expired"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        create_access_token(
            uuid.uuid4(),
            secret=SecretStr("another-secret-that-is-long-enough-0123"),
            ttl=timedelta(minutes=5),
        ),
        jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "type": "refresh",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            TEST_JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        ),
        jwt.encode(
            {
                "sub": "not-a-uuid",
                "type": "access",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            TEST_JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        ),
        jwt.encode(
            {"sub": str(uuid.uuid4()), "type": "access"},  # no exp
            TEST_JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        ),
    ],
    ids=["garbage", "wrong-secret", "wrong-type", "bad-subject", "missing-exp"],
)
def test_invalid_access_tokens(token: str) -> None:
    with pytest.raises(UnauthorizedError) as exc_info:
        decode_access_token(token, secret=SECRET)

    assert exc_info.value.code == "invalid_token"


def test_unsigned_token_is_rejected() -> None:
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "type": "access",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        key="",  # unsigned: the "none" algorithm takes no key
        algorithm="none",
    )

    with pytest.raises(UnauthorizedError):
        decode_access_token(token, secret=SECRET)


def test_refresh_tokens_are_random_and_hashed() -> None:
    first, second = generate_refresh_token(), generate_refresh_token()

    assert first != second
    assert hash_refresh_token(first) == hash_refresh_token(first)
    assert len(hash_refresh_token(first)) == 64
    assert first not in hash_refresh_token(first)
