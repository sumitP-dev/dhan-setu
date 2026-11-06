from typing import Annotated

from fastapi import Header

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=(
            "A new random value (e.g. a UUID) for each new action. Sending the same key "
            "again returns the first result instead of charging twice."
        ),
    ),
]
