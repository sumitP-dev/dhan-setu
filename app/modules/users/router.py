from fastapi import APIRouter, status

from app.common.schemas import error_responses
from app.modules.auth.dependencies import CurrentUser
from app.modules.users.dependencies import UserServiceDep
from app.modules.users.schemas import UserRead, UserUpdate

router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)


@router.get("/me")
async def get_me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("/me", responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT))
async def update_me(body: UserUpdate, user: CurrentUser, service: UserServiceDep) -> UserRead:
    updated = await service.update_profile(user, body)
    return UserRead.model_validate(updated)
