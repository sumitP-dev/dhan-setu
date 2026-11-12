"""Admin tasks run on the server, not over the API.

    python -m app.cli make-admin asha@example.com
    python -m app.cli remove-admin asha@example.com

There is deliberately no endpoint that grants the admin role: an attacker with a stolen
token must not be able to make themselves an admin.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import UserRole, UserStatus
from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.modules.users.models import User


class CliError(Exception):
    pass


async def set_role(session: AsyncSession, email: str, role: UserRole) -> User:
    user = await session.scalar(
        select(User).where(User.email == email.strip().lower()).with_for_update()
    )
    if user is None:
        msg = f"No account with email {email}"
        raise CliError(msg)
    if role is UserRole.ADMIN and user.status is not UserStatus.ACTIVE:
        msg = f"{email} is not active (status: {user.status}); verify the phone first"
        raise CliError(msg)
    user.role = role
    await session.commit()
    return user


async def _run(email: str, role: UserRole) -> None:
    engine = create_db_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            user = await set_role(session, email, role)
            print(f"{user.email} is now {user.role}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("make-admin", "give an existing, active account the admin role"),
        ("remove-admin", "make an admin a regular customer again"),
    ):
        commands.add_parser(name, help=help_text).add_argument("email")
    args = parser.parse_args(argv)
    role = UserRole.ADMIN if args.command == "make-admin" else UserRole.CUSTOMER
    try:
        asyncio.run(_run(args.email, role))
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
