from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService


def get_user_repository(db: DbSession) -> UserRepository:
    return UserRepository(db)


def get_user_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    return UserService(users)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
